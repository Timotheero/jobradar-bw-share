"""Connector for public employer Teamtailor RSS job feeds."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, local_query_match, string_tuple
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

_TENANT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class TeamtailorJobFeedConnector:
    """Read one employer's public Teamtailor RSS feed without API credentials."""

    MAX_ITEMS = 2_000

    def __init__(
        self,
        tenant: str,
        *,
        company: str | None = None,
        locale: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.tenant = _tenant_name(tenant)
        self.company = company
        self.locale = _locale(locale)
        self.base_url = f"https://{self.tenant}.teamtailor.com"
        self.source_info = SourceInfo(
            key=f"teamtailor:{self.tenant}",
            name=f"Teamtailor job feed ({self.tenant})",
            acquisition="official_public_feed",
            official=True,
            documentation_url="https://support.teamtailor.com/en/articles/7709931-job-listing-rss-feed",
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/rss+xml,application/xml,text/xml"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        feed_path = f"/{self.locale}/jobs.rss" if self.locale else "/jobs.rss"
        response = self._http.request("GET", feed_path)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ValueError("Teamtailor response is not valid XML") from exc

        page_size = query.pagination.page_size
        skip = (query.pagination.start_page - 1) * page_size
        page_count = query.pagination.max_pages or 1
        item_limit = min(page_size * page_count, self.MAX_ITEMS)
        examined = 0
        seen_ids: set[str] = set()
        company = self.company or _channel_text(root, "title")

        for item_index, item in enumerate(_items(root)):
            if item_index < skip:
                continue
            if examined >= item_limit:
                return
            examined += 1
            record = _normalize_teamtailor(self.source_info.key, company, item)
            if record is None or record.source_job_id in seen_ids:
                continue
            seen_ids.add(record.source_job_id)
            if local_query_match(record, query):
                yield record


def _tenant_name(value: str) -> str:
    candidate = value.strip().casefold()
    if "://" in candidate:
        parsed = urlparse(candidate)
        suffix = ".teamtailor.com"
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(suffix)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("tenant URL must be an HTTPS teamtailor.com origin")
        candidate = parsed.hostname[: -len(suffix)]
    if not _TENANT_PATTERN.fullmatch(candidate):
        raise ValueError("tenant must be a valid teamtailor.com subdomain")
    return candidate


def _locale(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not re.fullmatch(r"[A-Za-z]{2}(?:-[A-Za-z]{2})?", candidate):
        raise ValueError("Teamtailor locale must be a two-letter language or language-region code")
    return candidate


def _items(root: ElementTree.Element) -> Iterator[ElementTree.Element]:
    for element in root.iter():
        if _local_name(element.tag) == "item":
            yield element


def _normalize_teamtailor(
    source: str,
    company: str | None,
    item: ElementTree.Element,
) -> RawJobRecord | None:
    title = _child_text(item, "title")
    link = _https_url(_child_text(item, "link"))
    guid = _child_text(item, "guid")
    job_id = guid or _id_from_url(link)
    if not job_id or not title:
        return None

    description = _child_text(item, "encoded") or _child_text(item, "description")
    locations: list[str] = []
    for name in ("location", "jobLocation"):
        for value in _children_text(item, name):
            if value not in locations:
                locations.append(value)
    categories = _children_text(item, "category")
    employment = []
    for name in ("employmentType", "jobType"):
        employment.extend(value for value in _children_text(item, name) if value not in employment)
    remote = True if any("remote" in value.casefold() for value in locations + categories) else None

    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=tuple(locations),
        description=description,
        description_format="html" if description else None,
        canonical_url=link,
        apply_url=link,
        published_at=_rss_datetime(_child_text(item, "pubDate")),
        employment_types=string_tuple(employment),
        remote=remote,
        metadata={"categories": categories, "author": _child_text(item, "author")},
        raw={"xml": ElementTree.tostring(item, encoding="unicode")},
    )


def _channel_text(root: ElementTree.Element, name: str) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) == "channel":
            return _child_text(element, name)
    return None


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    values = _children_text(element, name)
    return values[0] if values else None


def _children_text(element: ElementTree.Element, name: str) -> list[str]:
    values: list[str] = []
    for child in element:
        if _local_name(child.tag) != name:
            continue
        text = _text(child.text)
        if text:
            values.append(text)
    return values


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else None


def _https_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def _rss_datetime(value: str | None):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
