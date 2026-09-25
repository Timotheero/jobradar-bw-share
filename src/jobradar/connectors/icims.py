"""Connector for public server-rendered iCIMS tenant job sites."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

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

_TENANT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_JOB_PATH = re.compile(r"/jobs/(?P<id>[A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)


class ICIMSJobBoardConnector:
    """Read one public iCIMS search site and bounded job detail pages."""

    MAX_PAGES = 100
    MAX_DETAIL_REQUESTS = 500

    def __init__(
        self,
        tenant: str,
        *,
        company: str | None = None,
        include_details: bool = True,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.tenant, self.base_url = _tenant_origin(tenant)
        self.company = company
        self.include_details = include_details
        self.source_info = SourceInfo(
            key=f"icims:{self.tenant}",
            name=f"iCIMS job board ({self.tenant})",
            acquisition="public_server_rendered_search",
            official=True,
            notes="Public employer search pages and embedded schema.org JobPosting data.",
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "text/html,application/xhtml+xml"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page = query.pagination.start_page
        page_limit = min(query.pagination.max_pages or self.MAX_PAGES, self.MAX_PAGES)
        seen_ids: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        detail_requests = 0

        for _ in range(page_limit):
            params: dict[str, Any] = {"ss": 1, "pr": page}
            if query.text:
                params["searchKeyword"] = query.text
            if query.location:
                params["searchLocation"] = query.location
            response = self._http.request("GET", "/jobs/search", params=params)
            search_html = response.text
            links = _job_links(search_html, self.base_url)
            page_signature = tuple(job_id for job_id, _, _ in links)
            if not links or page_signature in seen_pages:
                return
            seen_pages.add(page_signature)

            new_on_page = 0
            for job_id, detail_url, summary_title in links:
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                new_on_page += 1
                detail_html: str | None = None
                job_data: Mapping[str, Any] | None = None
                if self.include_details and detail_requests < self.MAX_DETAIL_REQUESTS:
                    detail_requests += 1
                    detail_response = self._http.request("GET", detail_url)
                    detail_html = detail_response.text
                    job_data = _job_posting(detail_html)
                record = _normalize_icims(
                    self.source_info.key,
                    self.company,
                    job_id,
                    detail_url,
                    summary_title,
                    search_html,
                    detail_html,
                    job_data,
                )
                if record is not None and local_query_match(record, query):
                    yield record

            if new_on_page == 0 or not _has_next_page(search_html, page, self.base_url):
                return
            page += 1


def _tenant_origin(value: str) -> tuple[str, str]:
    candidate = value.strip().casefold()
    if "://" not in candidate:
        if not _TENANT_PATTERN.fullmatch(candidate):
            raise ValueError("iCIMS tenant must be a valid icims.com subdomain")
        return candidate, f"https://{candidate}.icims.com"

    parsed = urlparse(candidate)
    suffix = ".icims.com"
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
        raise ValueError("iCIMS tenant URL must be an HTTPS icims.com origin")
    tenant = parsed.hostname[: -len(suffix)]
    if not _TENANT_PATTERN.fullmatch(tenant):
        raise ValueError("iCIMS tenant URL must contain one valid tenant subdomain")
    return tenant, f"https://{parsed.hostname}"


class _ICIMSHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str, Mapping[str, str]]] = []
        self.json_ld: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_attrs: dict[str, str] = {}
        self._anchor_text: list[str] = []
        self._json_script = False
        self._script_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "a" and attributes.get("href"):
            self._anchor_href = attributes["href"]
            self._anchor_attrs = attributes
            self._anchor_text = []
        elif (
            tag.casefold() == "script"
            and attributes.get("type", "").casefold() == "application/ld+json"
        ):
            self._json_script = True
            self._script_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._anchor_href is not None:
            text = " ".join("".join(self._anchor_text).split())
            self.anchors.append((self._anchor_href, text, dict(self._anchor_attrs)))
            self._anchor_href = None
            self._anchor_attrs = {}
            self._anchor_text = []
        elif tag.casefold() == "script" and self._json_script:
            self.json_ld.append("".join(self._script_text))
            self._json_script = False
            self._script_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._json_script:
            self._script_text.append(data)


def _parse_html(html: str) -> _ICIMSHTMLParser:
    parser = _ICIMSHTMLParser()
    parser.feed(html)
    parser.close()
    return parser


def _job_links(html: str, base_url: str) -> list[tuple[str, str, str | None]]:
    expected_host = urlparse(base_url).hostname
    results: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for href, text, attrs in _parse_html(html).anchors:
        url = urljoin(f"{base_url}/jobs/search", href)
        parsed = urlparse(url)
        match = _JOB_PATH.search(parsed.path)
        if (
            not match
            or parsed.scheme != "https"
            or parsed.hostname != expected_host
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        job_id = match.group("id")
        if job_id.casefold() in {"search", "login"} or job_id in seen:
            continue
        seen.add(job_id)
        title = text or attrs.get("title") or attrs.get("aria-label") or None
        canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        results.append((job_id, canonical, title))
    return results


def _has_next_page(html: str, current_page: int, base_url: str) -> bool:
    expected_host = urlparse(base_url).hostname
    for href, text, attrs in _parse_html(html).anchors:
        url = urljoin(f"{base_url}/jobs/search", href)
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != expected_host:
            continue
        pages = parse_qs(parsed.query).get("pr", [])
        link_description = " ".join(
            (text, attrs.get("rel", ""), attrs.get("aria-label", ""))
        )
        is_next = "next" in link_description.casefold()
        if any(_integer(value) == current_page + 1 for value in pages) or is_next:
            return True
    return False


def _job_posting(html: str) -> Mapping[str, Any] | None:
    for source in _parse_html(html).json_ld:
        try:
            payload = json.loads(source)
        except (json.JSONDecodeError, TypeError):
            continue
        for value in _json_objects(payload):
            object_type = value.get("@type")
            types = object_type if isinstance(object_type, list) else [object_type]
            if any(str(item).casefold() == "jobposting" for item in types):
                return value
    return None


def _json_objects(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_objects(item)


def _normalize_icims(
    source: str,
    configured_company: str | None,
    job_id: str,
    detail_url: str,
    summary_title: str | None,
    search_html: str,
    detail_html: str | None,
    job: Mapping[str, Any] | None,
) -> RawJobRecord | None:
    data: Mapping[str, Any] = job or {}
    title = _text(data.get("title")) or summary_title
    if not title:
        return None
    organization = data.get("hiringOrganization")
    company = configured_company
    if company is None and isinstance(organization, Mapping):
        company = _text(organization.get("name"))
    locations = _job_locations(data.get("jobLocation"))
    employment_types = string_tuple(data.get("employmentType"))
    canonical_url = _same_host_https(data.get("url"), detail_url) or detail_url

    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=locations,
        description=_text(data.get("description")),
        description_format="html" if data.get("description") else None,
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=parse_datetime(data.get("datePosted")),
        updated_at=parse_datetime(data.get("dateModified")),
        employment_types=employment_types,
        remote=_remote(data, locations),
        salary=_salary(data.get("baseSalary")),
        metadata={
            "identifier": data.get("identifier"),
            "valid_through": data.get("validThrough"),
            "direct_apply": data.get("directApply"),
        },
        raw={
            "search_html": search_html,
            "detail_html": detail_html,
            "json_ld": dict(job) if job is not None else None,
        },
    )


def _job_locations(value: Any) -> tuple[str, ...]:
    entries = value if isinstance(value, list) else [value]
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        address = entry.get("address")
        if isinstance(address, str):
            rendered = address.strip()
        elif isinstance(address, Mapping):
            rendered = ", ".join(
                part
                for part in (
                    _text(address.get("streetAddress")),
                    _text(address.get("postalCode")),
                    _text(address.get("addressLocality")),
                    _text(address.get("addressRegion")),
                    _country(address.get("addressCountry")),
                )
                if part
            )
        else:
            rendered = _text(entry.get("name")) or ""
        if rendered and rendered not in result:
            result.append(rendered)
    return tuple(result)


def _country(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return _text(value.get("name"))
    return _text(value)


def _remote(data: Mapping[str, Any], locations: tuple[str, ...]) -> bool | None:
    if (_text(data.get("jobLocationType")) or "").casefold() == "telecommute":
        return True
    if any("remote" in location.casefold() for location in locations):
        return True
    return None


def _salary(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return _text(value)
    currency = _text(value.get("currency"))
    amount = value.get("value")
    if isinstance(amount, Mapping):
        bounds = "–".join(
            str(item)
            for item in (amount.get("minValue"), amount.get("maxValue"))
            if item is not None
        )
        interval = _text(amount.get("unitText"))
    else:
        bounds = _text(amount) or ""
        interval = None
    return " ".join(part for part in (bounds, currency, interval) if part) or None


def _same_host_https(value: Any, reference: str) -> str | None:
    text = _text(value)
    if not text:
        return None
    parsed = urlparse(text)
    expected = urlparse(reference).hostname
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected
        or parsed.username
        or parsed.password
    ):
        return None
    return text


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
