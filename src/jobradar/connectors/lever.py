"""Connector for the public Lever Postings API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

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


class LeverJobBoardConnector:
    BASE_URL = "https://api.lever.co/v0"
    MAX_PAGE_SIZE = 100

    def __init__(
        self,
        site: str,
        *,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not site.strip():
            raise ValueError("Lever site must not be empty")
        self.site = site.strip()
        self.company = company
        self.source_info = SourceInfo(
            key=f"lever:{self.site}",
            name=f"Lever job board ({self.site})",
            acquisition="official_public_api",
            official=True,
            documentation_url="https://github.com/lever/postings-api",
        )
        self._http = HTTPTransport(
            self.BASE_URL,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page_size = min(query.pagination.page_size, self.MAX_PAGE_SIZE)
        page_index = max(0, query.pagination.start_page - 1)
        pages_read = 0

        while query.pagination.max_pages is None or pages_read < query.pagination.max_pages:
            params: dict[str, Any] = {
                "mode": "json",
                "limit": page_size,
                "skip": page_index * page_size,
            }
            if query.location:
                params["location"] = query.location
            params.update(query.filters)
            payload = self._http.request_json(
                "GET", f"/postings/{quote(self.site, safe='')}", params=params
            )
            jobs = _lever_items(payload)
            if not jobs:
                return
            for job in jobs:
                record = _normalize_lever(self.source_info.key, self.company, job)
                if record is not None and local_query_match(record, query):
                    yield record
            pages_read += 1
            if len(jobs) < page_size:
                return
            page_index += 1


def _lever_items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, Mapping)]
    raise TypeError("Lever response must be a JSON array or data envelope")


def _normalize_lever(
    source: str,
    company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _text(job.get("id"))
    title = _text(job.get("text"))
    if not job_id or not title:
        return None
    categories = job.get("categories") if isinstance(job.get("categories"), Mapping) else {}
    locations = string_tuple(categories.get("allLocations") or categories.get("location"))
    commitment = string_tuple(categories.get("commitment"))
    workplace = str(job.get("workplaceType") or "").casefold()
    remote = (
        True if workplace == "remote" else False if workplace in {"on-site", "onsite"} else None
    )
    description = _text(job.get("descriptionPlain"))
    description_format = "text"
    if description is None:
        description = _text(job.get("description"))
        description_format = "html"

    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=locations,
        description=description,
        description_format=description_format,
        canonical_url=_text(job.get("hostedUrl")),
        apply_url=_text(job.get("applyUrl")) or _text(job.get("hostedUrl")),
        published_at=parse_datetime(job.get("createdAt")),
        employment_types=commitment,
        remote=remote,
        salary=_salary(job.get("salaryRange")),
        metadata={
            "team": categories.get("team"),
            "department": categories.get("department"),
            "level": categories.get("level"),
            "workplace_type": job.get("workplaceType"),
        },
        raw=dict(job),
    )


def _salary(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return _text(value)
    minimum = value.get("min")
    maximum = value.get("max")
    currency = value.get("currency")
    interval = value.get("interval")
    bounds = "–".join(str(item) for item in (minimum, maximum) if item is not None)
    rendered = " ".join(str(item) for item in (bounds, currency, interval) if item)
    return rendered or None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
