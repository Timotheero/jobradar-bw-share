"""Connector for Arbeitnow's free public Germany job-board API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from ..models import Source


class ArbeitnowConnector:
    BASE_URL = "https://www.arbeitnow.com/api"
    source_info = SourceInfo(
        key="arbeitnow",
        name="Arbeitnow Job Board API",
        acquisition="official_public_api",
        official=True,
        documentation_url="https://www.arbeitnow.com/api/job-board-api",
        terms_url="https://www.arbeitnow.com/terms",
    )

    def __init__(
        self,
        *,
        stop_before: datetime | None = None,
        max_pages: int | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if max_pages is not None and max_pages < 1:
            raise ValueError("Arbeitnow max_pages must be at least 1")
        if stop_before is not None and stop_before.tzinfo is None:
            stop_before = stop_before.replace(tzinfo=UTC)
        self.stop_before = stop_before.astimezone(UTC) if stop_before else None
        self.max_pages = max_pages
        self._http = HTTPTransport(
            self.BASE_URL,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )
        self._diagnostics: dict[str, Any] = {
            "pages_read": 0,
            "records_emitted": 0,
            "cutoff_reached": False,
        }

    @classmethod
    def from_source(cls, source: Source) -> ArbeitnowConnector:
        metadata = source.metadata_json if isinstance(source.metadata_json, Mapping) else {}
        overlap_hours = _positive_integer(metadata.get("overlap_hours"), default=24)
        max_pages = _optional_positive_integer(metadata.get("max_pages"))
        stop_before = (
            source.last_success_at - timedelta(hours=overlap_hours)
            if source.last_success_at is not None
            else None
        )
        return cls(stop_before=stop_before, max_pages=max_pages)

    @property
    def run_diagnostics(self) -> Mapping[str, Any]:
        return self._diagnostics

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page = max(1, query.pagination.start_page)
        pages_read = 0
        page_limit = _minimum_optional(self.max_pages, query.pagination.max_pages)

        while page_limit is None or pages_read < page_limit:
            payload = self._http.request_json("GET", "/job-board-api", params={"page": page})
            if not isinstance(payload, Mapping):
                raise TypeError("Arbeitnow response must be a JSON object")
            items = payload.get("data")
            if not isinstance(items, list) or not items:
                return

            reached_cutoff = False
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                published_at = parse_datetime(item.get("created_at"))
                if (
                    self.stop_before is not None
                    and published_at is not None
                    and published_at < self.stop_before
                ):
                    reached_cutoff = True
                    continue
                record = _normalize_arbeitnow(item, published_at=published_at)
                if record is not None and local_query_match(record, query):
                    self._diagnostics["records_emitted"] += 1
                    yield record

            pages_read += 1
            self._diagnostics["pages_read"] = pages_read
            if reached_cutoff:
                self._diagnostics["cutoff_reached"] = True
                return
            links = payload.get("links")
            if isinstance(links, Mapping) and not links.get("next"):
                return
            page += 1


def _normalize_arbeitnow(
    job: Mapping[str, Any], *, published_at: datetime | None
) -> RawJobRecord | None:
    job_id = _text(job.get("slug"))
    title = _text(job.get("title"))
    if not job_id or not title:
        return None
    location = _text(job.get("location"))
    canonical_url = _text(job.get("url"))
    remote = job.get("remote") if isinstance(job.get("remote"), bool) else None
    return RawJobRecord(
        source=ArbeitnowConnector.source_info.key,
        source_job_id=job_id,
        title=title,
        company=_text(job.get("company_name")),
        locations=(location,) if location else (),
        description=_text(job.get("description")),
        description_format="html",
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=published_at,
        employment_types=string_tuple(job.get("job_types")),
        remote=remote,
        metadata={
            "country": "DE",
            "tags": list(string_tuple(job.get("tags"))),
            "remote_type": "remote" if remote else None,
        },
        raw=dict(job),
    )


def _minimum_optional(first: int | None, second: int | None) -> int | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def _positive_integer(value: Any, *, default: int) -> int:
    parsed = _optional_positive_integer(value)
    return parsed if parsed is not None else default


def _optional_positive_integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
