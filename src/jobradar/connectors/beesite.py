"""Connector for brand-bound public BeeSite JSON job searches."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
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

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class BeeSiteConnector:
    def __init__(
        self,
        brand: str,
        career_url: str,
        *,
        company: str | None = None,
        search_path: str = "/api/jobs",
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        brand = brand.strip().casefold()
        if not _TOKEN.fullmatch(brand):
            raise ValueError("BeeSite brand must be a safe provider identifier")
        self.base_url = _origin(career_url)
        self.search_path = _path(self.base_url, search_path)
        self.brand, self.company = brand, company.strip() if company and company.strip() else None
        self.source_info = SourceInfo(
            key=f"beesite:{brand}",
            name=f"BeeSite ({brand})",
            acquisition="official_public_career_api",
            official=True,
            documentation_url=self.base_url,
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        options = query.pagination if query else None
        size, start = (
            min(options.page_size if options else 50, 100),
            options.start_page if options else 1,
        )
        cap = min(options.max_pages or 100, 100) if options else 100
        seen: set[str] = set()
        for page in range(start, start + cap):
            params: dict[str, Any] = {"brand": self.brand, "page": page, "pageSize": size}
            if query and query.text:
                params["query"] = query.text
            if query and query.location:
                params["location"] = query.location
            payload = self._http.request_json("GET", self.search_path, params=params)
            if not isinstance(payload, Mapping):
                raise ValueError("BeeSite response must be a JSON object")
            rows = next(
                (
                    payload[k]
                    for k in ("jobs", "results", "items", "data")
                    if isinstance(payload.get(k), list)
                ),
                None,
            )
            if rows is None:
                raise ValueError("BeeSite response has no job list")
            if not rows:
                break
            new = 0
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                record = _normalize(self.source_info.key, self.base_url, self.company, row)
                if record is None or record.source_job_id in seen:
                    continue
                seen.add(record.source_job_id)
                new += 1
                if local_query_match(record, query):
                    yield record
            if new == 0 or len(rows) < size:
                break


def _normalize(
    source: str, base: str, company: str | None, row: Mapping[str, Any]
) -> RawJobRecord | None:
    job_id = _text(
        row.get("id") or row.get("jobId") or row.get("referenceNumber") or row.get("requisitionId")
    )
    title = _text(row.get("title") or row.get("jobTitle") or row.get("name"))
    if not job_id or not title:
        return None
    url = _url(base, _text(row.get("url") or row.get("detailUrl") or row.get("jobUrl")))
    description = _text(row.get("description") or row.get("summary"))
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company or _text(row.get("company") or row.get("brand")),
        locations=string_tuple(row.get("locations") or row.get("location")),
        description=description,
        description_format="html"
        if description and "<" in description
        else ("text" if description else None),
        canonical_url=url,
        apply_url=_url(base, _text(row.get("applyUrl"))) or url,
        published_at=parse_datetime(row.get("publishedAt") or row.get("datePosted")),
        updated_at=parse_datetime(row.get("updatedAt")),
        employment_types=string_tuple(row.get("employmentType") or row.get("jobType")),
        raw=dict(row),
    )


def _origin(value: str) -> str:
    p = urlparse(value.strip())
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username
        or p.password
        or p.port not in (None, 443)
        or "." not in p.hostname
        or p.hostname.casefold().endswith(".localhost")
    ):
        raise ValueError("BeeSite career_url must use a public HTTPS host on port 443")
    return f"https://{p.hostname.casefold()}"


def _path(base: str, value: str) -> str:
    p = urlparse(urljoin(base + "/", value))
    if p.scheme != "https" or p.netloc.casefold() != urlparse(base).netloc.casefold():
        raise ValueError("BeeSite search_path must remain on the career host")
    return p.path + (f"?{p.query}" if p.query else "")


def _url(base: str, value: str | None) -> str | None:
    if not value:
        return None
    p = urlparse(urljoin(base + "/", value))
    return p.geturl() if p.scheme == "https" and p.hostname == urlparse(base).hostname else None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
