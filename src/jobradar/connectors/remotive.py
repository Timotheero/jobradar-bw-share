"""Connector for Remotive's free public active-remote-jobs API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

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
from .remote_eligibility import available_in_germany


class RemotiveConnector:
    BASE_URL = "https://remotive.com"
    source_info = SourceInfo(
        key="remotive_remote",
        name="Remotive Remote Jobs API",
        acquisition="official_public_api",
        official=True,
        documentation_url="https://github.com/remotive-com/remote-jobs-api",
        terms_url="https://github.com/remotive-com/remote-jobs-api#terms-of-services",
    )

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._http = HTTPTransport(
            self.BASE_URL,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )
        self._diagnostics: dict[str, Any] = {
            "location_filtered": 0,
            "records_emitted": 0,
        }

    @property
    def run_diagnostics(self) -> Mapping[str, Any]:
        return self._diagnostics

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        payload = self._http.request_json("GET", "/api/remote-jobs")
        if not isinstance(payload, Mapping):
            raise TypeError("Remotive response must be a JSON object")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            return
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            location = _text(job.get("candidate_required_location"))
            if not available_in_germany(location):
                self._diagnostics["location_filtered"] += 1
                continue
            record = _normalize_remotive(job, location=location)
            if record is not None and local_query_match(record, query):
                self._diagnostics["records_emitted"] += 1
                yield record


def _normalize_remotive(
    job: Mapping[str, Any], *, location: str | None
) -> RawJobRecord | None:
    job_id = _text(job.get("id"))
    title = _text(job.get("title"))
    if not job_id or not title:
        return None
    canonical_url = _text(job.get("url"))
    return RawJobRecord(
        source=RemotiveConnector.source_info.key,
        source_job_id=job_id,
        title=title,
        company=_text(job.get("company_name")),
        locations=(location,) if location else ("Remote",),
        description=_text(job.get("description")),
        description_format="html",
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=parse_datetime(job.get("publication_date")),
        employment_types=string_tuple(job.get("job_type")),
        remote=True,
        salary=_text(job.get("salary")),
        metadata={
            "country": "DE",
            "remote_type": "remote",
            "category": _text(job.get("category")),
        },
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
