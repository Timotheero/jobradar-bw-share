"""Connector for public employer career sites operated by Radancy."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .base import (
    JobQuery,
    RawJobRecord,
    SourceInfo,
    local_query_match,
    parse_datetime,
    string_tuple,
)
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_PAGES = 100


class RadancyConnector:
    """Read one explicitly configured, public Radancy employer search."""

    def __init__(
        self,
        identifier: str,
        career_url: str,
        *,
        company: str | None = None,
        search_path: str = "/search-jobs/results",
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        identifier = identifier.strip().casefold()
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("Radancy identifier must contain only letters, digits, '_' or '-'")
        self.base_url = _public_https_origin(career_url, "Radancy career_url")
        self.search_path = _same_host_path(self.base_url, search_path, "Radancy search_path")
        self.identifier = identifier
        self.company = company.strip() if company and company.strip() else None
        self.source_info = SourceInfo(
            key=f"radancy:{identifier}",
            name=f"Radancy career site ({identifier})",
            acquisition="official_public_career_site",
            official=True,
            documentation_url=self.base_url,
        )
        self._http = HTTPTransport(self.base_url, client=client, timeout=timeout, retry=retry)

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        pagination = query.pagination if query else None
        page_size = min(pagination.page_size if pagination else 50, 100)
        start = pagination.start_page if pagination else 1
        cap = min(pagination.max_pages or _MAX_PAGES, _MAX_PAGES) if pagination else _MAX_PAGES
        seen: set[str] = set()
        for page in range(start, start + cap):
            params: dict[str, Any] = {"page": page, "pageSize": page_size}
            if query and query.text:
                params["keywords"] = query.text
            if query and query.location:
                params["location"] = query.location
            response = self._http.request("GET", self.search_path, params=params)
            rows = _radancy_rows(response)
            if not rows:
                break
            new = 0
            for row in rows:
                record = _normalize(self.source_info.key, self.base_url, self.company, row)
                if record is None or record.source_job_id in seen:
                    continue
                seen.add(record.source_job_id)
                new += 1
                if local_query_match(record, query):
                    yield record
            if new == 0 or len(rows) < page_size:
                break


def _radancy_rows(response: httpx.Response) -> list[Mapping[str, Any]]:
    content_type = response.headers.get("content-type", "").casefold()
    if "json" in content_type or response.text.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError("Radancy response declares JSON but is malformed") from exc
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        if not isinstance(payload, Mapping):
            raise ValueError("Radancy JSON response must be an object or list")
        for key in ("jobs", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
        for key in ("html", "resultsHtml", "searchResults", "markup"):
            value = payload.get(key)
            if isinstance(value, str):
                return _parse_cards(value)
        raise ValueError("Radancy JSON response contains neither jobs nor an HTML fragment")
    return _parse_cards(response.text)


def _normalize(
    source: str, base_url: str, company: str | None, row: Mapping[str, Any]
) -> RawJobRecord | None:
    title = _text(row.get("title") or row.get("name") or row.get("jobTitle"))
    href = _text(row.get("url") or row.get("jobUrl") or row.get("href"))
    job_id = _text(
        row.get("id") or row.get("jobId") or row.get("requisitionId") or row.get("reqId")
    )
    if not job_id and href:
        job_id = _id_from_url(href)
    if not title or not job_id:
        return None
    url = _canonical_url(base_url, href)
    location = row.get("locations") or row.get("location") or row.get("city")
    description = _text(row.get("description") or row.get("summary"))
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company or _text(row.get("company") or row.get("brand")),
        locations=string_tuple(location),
        description=description,
        description_format="html"
        if description and "<" in description
        else ("text" if description else None),
        canonical_url=url,
        apply_url=_canonical_url(base_url, _text(row.get("applyUrl"))) or url,
        published_at=parse_datetime(row.get("datePosted") or row.get("postedDate")),
        updated_at=parse_datetime(row.get("updatedDate")),
        employment_types=string_tuple(row.get("employmentType") or row.get("jobType")),
        raw=dict(row),
    )


class _CardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._link: dict[str, Any] | None = None
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self._link is not None:
            self._depth += 1
            return
        href = values.get("href")
        if tag == "a" and href and re.search(r"job|position|vacan|career", href, re.I):
            self._link = {"href": href, "id": values.get("data-job-id") or values.get("data-id")}
            self._depth = 1
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._link is None:
            return
        self._depth -= 1
        if self._depth == 0:
            title = " ".join(" ".join(self._parts).split())
            if title:
                self._link["title"] = title
                self.rows.append(self._link)
            self._link = None

    def handle_data(self, data: str) -> None:
        if self._link is not None and data.strip():
            self._parts.append(data.strip())


def _parse_cards(html: str) -> list[Mapping[str, Any]]:
    parser = _CardParser()
    parser.feed(html)
    return parser.rows


def _public_https_origin(value: str, label: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"{label} must be a public HTTPS URL on port 443")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        raise ValueError(f"{label} must use a public hostname")
    return f"https://{host}"


def _same_host_path(base_url: str, value: str, label: str) -> str:
    parsed = urlparse(urljoin(base_url + "/", value))
    if parsed.scheme != "https" or parsed.netloc.casefold() != urlparse(base_url).netloc.casefold():
        raise ValueError(f"{label} must remain on the configured HTTPS career host")
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _canonical_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(urljoin(base_url + "/", value))
    if parsed.scheme != "https" or parsed.hostname != urlparse(base_url).hostname:
        return None
    return parsed.geturl()


def _id_from_url(value: str) -> str | None:
    path = urlparse(value).path.rstrip("/")
    match = re.search(r"(?:-|/)([A-Za-z0-9][A-Za-z0-9_-]{2,})$", path)
    return match.group(1) if match else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = unescape(str(value)).strip()
    return text or None
