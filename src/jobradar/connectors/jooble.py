"""Connector for Jooble's public job-search aggregation API."""

from __future__ import annotations

import hashlib
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
    from ..config import Settings
    from ..models import Source


class JoobleConnector:
    BASE_URL = "https://jooble.org"
    source_info = SourceInfo(
        key="jooble",
        name="Jooble Job API",
        acquisition="official_public_api",
        official=True,
        documentation_url="https://jooble.org/api/about",
    )

    def __init__(
        self,
        api_key: str,
        *,
        stop_before: datetime | None = None,
        max_pages: int | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Jooble API key is required")
        if max_pages is not None and max_pages < 1:
            raise ValueError("Jooble max_pages must be at least 1")
        if stop_before is not None and stop_before.tzinfo is None:
            stop_before = stop_before.replace(tzinfo=UTC)
        self.stop_before = stop_before.astimezone(UTC) if stop_before else None
        self.max_pages = max_pages
        self._api_key = api_key
        self.keyword_groups: list[str] = [
            "Assistenz der Geschäftsführung Executive Assistant Assistenz",
            "Assistant Geschäftsführung Geschäftsleitung",
            "Referent Vorstandsassistenz Referent des Vorstands",
            "Office Manager Chief of Staff CEO Office",
            "Projektkoordination Stabsmitarbeiter Management Coordinator",
            "Executive Office",
        ]
        # Jooble API endpoint is https://jooble.org/api/{API_KEY}
        # We use an empty base_url and pass the full URL into request_json
        self._http = HTTPTransport(
            "",
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Content-Type": "application/json"},
        )
        self._diagnostics: dict[str, Any] = {
            "pages_read": 0,
            "records_emitted": 0,
            "cutoff_reached": False,
        }

    @classmethod
    def from_source(cls, source: Source) -> JoobleConnector:
        from ..config import get_settings

        settings: Settings = get_settings()
        api_key = settings.jooble_api_key
        if not api_key:
            raise ValueError(
                "JOOBLE_API_KEY is not configured — set it in the environment or .env"
            )
        metadata = source.metadata_json if isinstance(source.metadata_json, Mapping) else {}
        overlap_hours = _positive_integer(metadata.get("overlap_hours"), default=24)
        max_pages = _optional_positive_integer(metadata.get("max_pages"))
        stop_before = (
            source.last_success_at - timedelta(hours=overlap_hours)
            if source.last_success_at is not None
            else None
        )
        return cls(api_key=api_key, stop_before=stop_before, max_pages=max_pages)

    @property
    def run_diagnostics(self) -> Mapping[str, Any]:
        return self._diagnostics

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        """Iterate through all keyword groups, each yielding up to max_pages."""
        query = query or JobQuery()
        groups = self.keyword_groups

        if not self._diagnostics.get("group_labels"):
            self._diagnostics["group_labels"] = []

        for keywords in groups:
            group_label = keywords[:40]
            self._diagnostics["group_labels"].append(group_label)
            yield from self._search_keywords(keywords, query)

    def _search_keywords(self, keywords: str, query: JobQuery) -> Iterator[RawJobRecord]:
        page = max(1, query.pagination.start_page)
        pages_read = 0
        page_limit = _minimum_optional(self.max_pages, query.pagination.max_pages)

        api_url = f"{self.BASE_URL}/api/{self._api_key}"

        while page_limit is None or pages_read < page_limit:
            body: dict[str, Any] = {
                "keywords": keywords,
                "location": query.location if query.location else "Germany",
                "page": page,
            }
            payload = self._http.request_json("POST", api_url, json=body)
            if not isinstance(payload, Mapping):
                raise TypeError("Jooble response must be a JSON object")

            items = payload.get("jobs")
            if not isinstance(items, list) or not items:
                return

            total_count = payload.get("totalCount")
            total_count = int(total_count) if total_count is not None else None

            reached_cutoff = False
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                updated = item.get("updated")
                published_at = parse_datetime(updated) if updated else None
                if (
                    self.stop_before is not None
                    and published_at is not None
                    and published_at < self.stop_before
                ):
                    reached_cutoff = True
                    continue
                record = _normalize_jooble(item, published_at=published_at)
                if record is not None and local_query_match(record, query):
                    self._diagnostics["records_emitted"] += 1
                    yield record

            pages_read += 1
            self._diagnostics["pages_read"] = self._diagnostics.get("pages_read", 0) + 1

            if reached_cutoff:
                self._diagnostics["cutoff_reached"] = True
                return

            if total_count is not None:
                emitted_so_far = pages_read * len(items)
                if emitted_so_far >= total_count:
                    return

            page += 1


def _normalize_jooble(
    job: Mapping[str, Any], *, published_at: datetime | None
) -> RawJobRecord | None:
    link = _text(job.get("link"))
    if not link:
        return None
    job_id = hashlib.sha256(link.encode("utf-8")).hexdigest()[:24]
    title = _text(job.get("title"))
    if not title:
        return None

    location = _text(job.get("location"))
    salary = _text(job.get("salary"))
    description = _text(job.get("snippet"))
    employment_types = string_tuple(job.get("type"))
    source_name = _text(job.get("source"))

    metadata: dict[str, Any] = {}
    if source_name:
        metadata["jooble_source"] = source_name

    return RawJobRecord(
        source=JoobleConnector.source_info.key,
        source_job_id=job_id,
        title=title,
        locations=(location,) if location else (),
        description=description,
        description_format="text",
        apply_url=link,
        canonical_url=link,
        published_at=published_at,
        salary=salary,
        employment_types=employment_types,
        metadata=metadata,
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