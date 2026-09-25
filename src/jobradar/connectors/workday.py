"""Connector for public Workday CXS career-site searches."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote, urlsplit

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

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_WORKDAY_HOST_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9-]+\.)?(?:myworkdayjobs\.com|workdayjobs\.com)$", re.IGNORECASE
)


class WorkdayJobBoardConnector:
    """Read a configured tenant/site through its unauthenticated CXS endpoint."""

    MAX_PAGE_SIZE = 100
    DEFAULT_MAX_PAGES = 100

    def __init__(
        self,
        careers_url: str,
        *,
        tenant: str,
        site: str,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self.base_url = _validated_workday_url(careers_url)
        self.tenant = _identifier("tenant", tenant)
        self.site = _identifier("site", site)
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self.max_pages = max_pages
        self.company = company.strip() if company and company.strip() else None
        self.source_info = SourceInfo(
            key=f"workday:{self.tenant}:{self.site}",
            name=f"Workday job board ({self.tenant}/{self.site})",
            acquisition="public_cxs_api",
            official=True,
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page_size = min(query.pagination.page_size, self.MAX_PAGE_SIZE)
        offset = (query.pagination.start_page - 1) * page_size
        page_limit = min(query.pagination.max_pages or self.max_pages, self.max_pages)
        seen: set[str] = set()
        path = f"/wday/cxs/{quote(self.tenant, safe='')}/{quote(self.site, safe='')}/jobs"

        for _ in range(page_limit):
            facets = query.filters.get("appliedFacets", {})
            if not isinstance(facets, Mapping):
                raise ValueError("Workday appliedFacets filter must be a mapping")
            request_payload = {
                "appliedFacets": dict(facets),
                "limit": page_size,
                "offset": offset,
                "searchText": query.text or "",
            }
            payload = self._http.request_json("POST", path, json=request_payload, retryable=True)
            jobs, total = _workday_jobs(payload)
            if not jobs:
                return
            unseen = 0
            for job in jobs:
                record = _normalize_workday(
                    self.source_info.key, self.base_url, self.site, self.company, job
                )
                if record is None or record.source_job_id in seen:
                    continue
                seen.add(record.source_job_id)
                unseen += 1
                if local_query_match(record, query):
                    yield record
            offset += len(jobs)
            if unseen == 0 or len(jobs) < page_size or (total is not None and offset >= total):
                return


def _validated_workday_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Workday careers_url must be an HTTPS URL without credentials")
    if parsed.port not in (None, 443):
        raise ValueError("Workday careers_url must use the default HTTPS port")
    hostname = parsed.hostname.rstrip(".").lower()
    if not _WORKDAY_HOST_PATTERN.fullmatch(hostname):
        raise ValueError("Workday careers_url must use a Workday careers host")
    if parsed.query or parsed.fragment:
        raise ValueError("Workday careers_url must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    return f"https://{hostname}{path}"


def _identifier(name: str, value: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(f"Workday {name} contains unsupported characters")
    return normalized


def _workday_jobs(payload: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(payload, Mapping):
        raise TypeError("Workday response must be a JSON object")
    raw_jobs = payload.get("jobPostings")
    if not isinstance(raw_jobs, list):
        raise TypeError("Workday response must contain a jobPostings array")
    total = payload.get("total")
    parsed_total = total if isinstance(total, int) and not isinstance(total, bool) else None
    return [item for item in raw_jobs if isinstance(item, Mapping)], parsed_total


def _normalize_workday(
    source: str,
    base_url: str,
    site: str,
    company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    external_path = _text(job.get("externalPath"))
    bullet_fields = job.get("bulletFields")
    job_id = _text(
        bullet_fields[-1] if isinstance(bullet_fields, list) and bullet_fields else None
    )
    if external_path:
        job_id = _text(job.get("id")) or external_path.rstrip("/").rsplit("/", 1)[-1] or job_id
    else:
        job_id = _text(job.get("id")) or job_id
    title = _text(job.get("title"))
    if not job_id or not title:
        return None

    canonical_url = None
    if external_path:
        canonical_url = f"{base_url}/{quote(site, safe='')}/{external_path.lstrip('/')}"
    locations_value = job.get("locations") or job.get("location")
    locations = string_tuple(locations_value)
    if not locations:
        locations = string_tuple(job.get("displayLocation"))
    description = _text(job.get("description"))
    employment = job.get("timeType") or job.get("employmentType")
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=locations,
        description=description,
        description_format="html" if description else None,
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=parse_datetime(job.get("postedOn") or job.get("postedDate")),
        employment_types=string_tuple(employment),
        metadata={"external_path": external_path, "bullet_fields": job.get("bulletFields")},
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
