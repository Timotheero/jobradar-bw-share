"""Connector for Adzuna's official Germany job-search API."""

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
)
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

if TYPE_CHECKING:
    from ..config import Settings
    from ..models import Source


class AdzunaConnector:
    """Read jobs from the Adzuna Germany job-search API (v1)."""

    BASE_URL = "https://api.adzuna.com"
    source_info = SourceInfo(
        key="adzuna",
        name="Adzuna Job Search API",
        acquisition="official_public_api",
        official=True,
        documentation_url="https://developer.adzuna.com/",
        terms_url="https://www.adzuna.de/legal/api-terms",
    )

    def __init__(
        self,
        *,
        app_id: str,
        api_key: str,
        stop_before: datetime | None = None,
        max_pages: int | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not app_id or not api_key:
            raise ValueError("AdzunaConnector requires app_id and api_key")
        if max_pages is not None and max_pages < 1:
            raise ValueError("Adzuna max_pages must be at least 1")
        if stop_before is not None and stop_before.tzinfo is None:
            stop_before = stop_before.replace(tzinfo=UTC)
        self._app_id = app_id
        self._api_key = api_key
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
    def from_source(cls, source: Source) -> AdzunaConnector:
        from ..config import get_settings

        settings: Settings = get_settings()
        metadata = (
            source.metadata_json
            if isinstance(source.metadata_json, Mapping)
            else {}
        )
        overlap_hours = _positive_integer(
            metadata.get("overlap_hours"), default=24
        )
        max_pages = _optional_positive_integer(metadata.get("max_pages"))
        stop_before = (
            source.last_success_at - timedelta(hours=overlap_hours)
            if source.last_success_at is not None
            else None
        )
        return cls(
            app_id=settings.adzuna_app_id,
            api_key=settings.adzuna_api_key,
            stop_before=stop_before,
            max_pages=max_pages,
        )

    @property
    def run_diagnostics(self) -> Mapping[str, Any]:
        return dict(self._diagnostics)

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page = max(1, query.pagination.start_page)
        pages_read = 0
        page_limit = _minimum_optional(self.max_pages, query.pagination.max_pages)

        while page_limit is None or pages_read < page_limit:
            params: dict[str, Any] = {
                "app_id": self._app_id,
                "app_key": self._api_key,
                "results_per_page": 50,
                "content-type": "application/json",
                "max_days_old": 30,
            }
            payload = self._http.request_json(
                "GET", f"/v1/api/jobs/de/search/{page}", params=params
            )
            if not isinstance(payload, Mapping):
                raise TypeError("Adzuna response must be a JSON object")
            items = payload.get("results")
            if not isinstance(items, list) or not items:
                return

            reached_cutoff = False
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                published_at = parse_datetime(item.get("created"))
                if (
                    self.stop_before is not None
                    and published_at is not None
                    and published_at < self.stop_before
                ):
                    reached_cutoff = True
                    continue
                record = _normalize_adzuna(item, published_at=published_at)
                if record is not None and local_query_match(record, query):
                    self._diagnostics["records_emitted"] += 1
                    yield record

            pages_read += 1
            self._diagnostics["pages_read"] = pages_read
            if reached_cutoff:
                self._diagnostics["cutoff_reached"] = True
                return
            page += 1


def _normalize_adzuna(
    job: Mapping[str, Any], *, published_at: datetime | None
) -> RawJobRecord | None:
    job_id = _text(str(job.get("id"))) if job.get("id") is not None else None
    title = _text(job.get("title"))
    if not job_id or not title:
        return None

    company_raw = job.get("company")
    company = (
        _text(company_raw.get("display_name"))
        if isinstance(company_raw, Mapping)
        else None
    )

    location_raw = job.get("location")
    location = (
        _text(location_raw.get("display_name"))
        if isinstance(location_raw, Mapping)
        else None
    )

    description = _text(job.get("description"))
    apply_url = _text(job.get("redirect_url"))

    salary = _format_adzuna_salary(job.get("salary_min"), job.get("salary_max"))

    employment_types: list[str] = []
    contract_type = _text(job.get("contract_type"))
    if contract_type:
        employment_types.append(contract_type)
    contract_time = _text(job.get("contract_time"))
    if contract_time and contract_time.casefold() in ("full_time", "part_time"):
        employment_types.append(contract_time)

    category_raw = job.get("category")
    adzuna_category = (
        _text(category_raw.get("label"))
        if isinstance(category_raw, Mapping)
        else None
    )

    metadata: dict[str, Any] = {}
    if adzuna_category:
        metadata["adzuna_category"] = adzuna_category
    if salary:
        metadata["salary"] = salary

    return RawJobRecord(
        source=AdzunaConnector.source_info.key,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=(location,) if location else (),
        description=description,
        description_format="html" if description else None,
        apply_url=apply_url,
        published_at=published_at,
        employment_types=tuple(employment_types),
        salary=salary,
        raw=dict(job),
    )


def _format_adzuna_salary(
    salary_min: Any, salary_max: Any
) -> str | None:
    """Format salary range as '€X.XXX - €Y.XXX'."""
    if salary_min is None and salary_max is None:
        return None

    def _format_value(value: Any) -> str | None:
        if value is None:
            return None
        try:
            num = int(float(value))
            return f"€{num:,}".replace(",", ".")
        except (ValueError, TypeError):
            return None

    min_fmt = _format_value(salary_min)
    max_fmt = _format_value(salary_max)

    if min_fmt and max_fmt:
        return f"{min_fmt} - {max_fmt}"
    if min_fmt:
        return min_fmt
    if max_fmt:
        return max_fmt
    return None


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
    except (ValueError, TypeError):
        return None
    return parsed if parsed >= 1 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None