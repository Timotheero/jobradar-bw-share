"""Connector for TKMS's public job-board JSON search API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urljoin

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


class TKMSConnector:
    source_info = SourceInfo(
        key="tkms",
        name="TKMS Jobboard",
        acquisition="official_public_career_api",
        official=True,
        documentation_url="https://jobs.tkmsgroup.com/en",
    )

    def __init__(
        self,
        *,
        locale: str = "en",
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        locale = locale.strip().casefold()
        if locale not in {"de", "en"}:
            raise ValueError("TKMS locale must be 'de' or 'en'")
        self.locale = locale
        self._http = HTTPTransport(
            "https://jobs.tkmsgroup.com",
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        options = query.pagination if query else None
        start = (options.start_page - 1) if options else 0
        cap = min(options.max_pages or 100, 100) if options else 100
        seen: set[str] = set()
        for page in range(start, start + cap):
            payload = self._http.request_json(
                "POST",
                "/api/filter/query",
                json={
                    "searchQuery": query.text if query and query.text else "",
                    "filter": {},
                    "subclient": "tkms",
                    "locale": self.locale,
                    "page": page,
                },
                retryable=True,
            )
            if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
                raise ValueError("TKMS response must contain a jobs list")
            rows = payload["jobs"]
            if not rows:
                break
            new = 0
            for wrapper in rows:
                if not isinstance(wrapper, Mapping):
                    continue
                data = wrapper.get("data")
                if not isinstance(data, Mapping):
                    continue
                record = _normalize(self.locale, data, wrapper)
                if record is None or record.source_job_id in seen:
                    continue
                seen.add(record.source_job_id)
                new += 1
                if local_query_match(record, query):
                    yield record
            next_page = payload.get("nextPage")
            if new == 0 or next_page is None:
                break


def _normalize(
    locale: str, data: Mapping[str, Any], wrapper: Mapping[str, Any]
) -> RawJobRecord | None:
    job_id, title = _text(data.get("id") or data.get("idClient")), _text(data.get("title"))
    if not job_id or not title:
        return None
    locations = data.get("locations") or data.get("cityState") or data.get("city")
    canonical = urljoin(f"https://jobs.tkmsgroup.com/{locale}/", f"job/{job_id}")
    application = _text(data.get("applicationUrl"))
    raw = dict(wrapper)
    raw["data"] = dict(data)
    salary_info = data.get("salaryInfo")
    salary = (
        str(dict(salary_info))
        if isinstance(salary_info, Mapping) and any(salary_info.values())
        else None
    )
    remote_value = (_text(data.get("remote")) or "").casefold()
    return RawJobRecord(
        source="tkms",
        source_job_id=job_id,
        title=title,
        company=_text(data.get("company")),
        locations=string_tuple(locations),
        canonical_url=canonical,
        apply_url=urljoin("https://jobs.tkmsgroup.com", application) if application else canonical,
        published_at=parse_datetime(data.get("postingDate")),
        employment_types=string_tuple(data.get("employmentType")),
        remote=True if "hybrid" in remote_value or "remote" in remote_value else None,
        salary=salary,
        metadata={
            "contract": data.get("contract"),
            "entry_level": data.get("entryLevel"),
            "job_field": data.get("jobField"),
            "job_number": data.get("jobNumber"),
        },
        raw=raw,
    )


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
