"""Connector for Jobicy's free public remote-jobs API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class JobicyFeedQuery:
    industry: str | None = None
    tag: str | None = None
    geo: str | None = None


DEFAULT_FEED_QUERIES = (
    JobicyFeedQuery(industry="admin-support"),
    JobicyFeedQuery(tag="chief of staff"),
    JobicyFeedQuery(tag="vorstandsassistenz"),
)


class JobicyConnector:
    BASE_URL = "https://jobicy.com/api/v2"
    source_info = SourceInfo(
        key="jobicy_remote",
        name="Jobicy Remote Jobs API",
        acquisition="official_public_api",
        official=True,
        documentation_url="https://jobicy.com/jobs-rss-feed",
        terms_url="https://jobicy.com/jobs-rss-feed#section7",
    )

    def __init__(
        self,
        *,
        feed_queries: tuple[JobicyFeedQuery, ...] = DEFAULT_FEED_QUERIES,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not feed_queries:
            raise ValueError("Jobicy requires at least one feed query")
        self.feed_queries = feed_queries
        self._http = HTTPTransport(
            self.BASE_URL,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )
        self._diagnostics: dict[str, Any] = {
            "query_count": len(feed_queries),
            "requests": 0,
            "duplicates_skipped": 0,
            "location_filtered": 0,
            "records_emitted": 0,
        }

    @property
    def run_diagnostics(self) -> Mapping[str, Any]:
        return self._diagnostics

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        emitted_ids: set[str] = set()
        for feed_query in self.feed_queries:
            params = {
                "count": 100,
                "industry": feed_query.industry,
                "tag": feed_query.tag,
                "geo": feed_query.geo,
            }
            payload = self._http.request_json(
                "GET",
                "/remote-jobs",
                params={key: value for key, value in params.items() if value is not None},
            )
            self._diagnostics["requests"] += 1
            if not isinstance(payload, Mapping):
                raise TypeError("Jobicy response must be a JSON object")
            jobs = payload.get("jobs")
            if not isinstance(jobs, list):
                continue
            for job in jobs:
                if not isinstance(job, Mapping):
                    continue
                location = _text(job.get("jobGeo"))
                if not available_in_germany(location):
                    self._diagnostics["location_filtered"] += 1
                    continue
                record = _normalize_jobicy(job, location=location)
                if record is None:
                    continue
                if record.source_job_id in emitted_ids:
                    self._diagnostics["duplicates_skipped"] += 1
                    continue
                emitted_ids.add(record.source_job_id)
                if local_query_match(record, query):
                    self._diagnostics["records_emitted"] += 1
                    yield record


def _normalize_jobicy(
    job: Mapping[str, Any], *, location: str | None
) -> RawJobRecord | None:
    job_id = _text(job.get("id"))
    title = _text(job.get("jobTitle"))
    if not job_id or not title:
        return None
    canonical_url = _text(job.get("url"))
    return RawJobRecord(
        source=JobicyConnector.source_info.key,
        source_job_id=job_id,
        title=title,
        company=_text(job.get("companyName")),
        locations=(location,) if location else ("Remote",),
        description=_text(job.get("jobDescription")),
        description_format="html",
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=parse_datetime(job.get("pubDate")),
        employment_types=string_tuple(job.get("jobType")),
        remote=True,
        salary=_salary(job),
        metadata={
            "country": "DE",
            "remote_type": "remote",
            "industry": list(string_tuple(job.get("jobIndustry"))),
            "level": _text(job.get("jobLevel")),
        },
        raw=dict(job),
    )


def _salary(job: Mapping[str, Any]) -> str | None:
    minimum = _text(job.get("annualSalaryMin") or job.get("salaryMin"))
    maximum = _text(job.get("annualSalaryMax") or job.get("salaryMax"))
    if not minimum and not maximum:
        return None
    amount = minimum if minimum == maximum or maximum is None else f"{minimum or '0'}–{maximum}"
    currency = _text(job.get("salaryCurrency"))
    period = _text(job.get("salaryPeriod"))
    return " ".join(part for part in (amount, currency, period) if part)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
