"""Connector for Ashby's public Job Posting API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote, urlparse

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


class AshbyJobBoardConnector:
    BASE_URL = "https://api.ashbyhq.com/posting-api"

    def __init__(
        self,
        board_name: str,
        *,
        company: str | None = None,
        include_compensation: bool = True,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not board_name.strip():
            raise ValueError("Ashby board_name must not be empty")
        self.board_name = board_name.strip()
        self.company = company
        self.include_compensation = include_compensation
        self.source_info = SourceInfo(
            key=f"ashby:{self.board_name}",
            name=f"Ashby job board ({self.board_name})",
            acquisition="official_public_api",
            official=True,
            documentation_url="https://developers.ashbyhq.com/docs/public-job-posting-api",
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
        payload = self._http.request_json(
            "GET",
            f"/job-board/{quote(self.board_name, safe='')}",
            params={"includeCompensation": str(self.include_compensation).lower()},
        )
        if not isinstance(payload, Mapping):
            raise TypeError("Ashby response must be a JSON object")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            return
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            record = _normalize_ashby(self.source_info.key, self.company, job)
            if record is not None and local_query_match(record, query):
                yield record


def _normalize_ashby(
    source: str,
    company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_url = _text(job.get("jobUrl"))
    job_id = _text(job.get("id")) or _id_from_url(job_url)
    title = _text(job.get("title"))
    if not job_id or not title:
        return None
    locations: list[str] = list(string_tuple(job.get("location")))
    for location in string_tuple(job.get("secondaryLocations")):
        if location not in locations:
            locations.append(location)
    description = _text(job.get("descriptionPlain"))
    description_format = "text"
    if description is None:
        description = _text(job.get("descriptionHtml"))
        description_format = "html"
    remote = job.get("isRemote") if isinstance(job.get("isRemote"), bool) else None
    employment_types = string_tuple(job.get("employmentType"))
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=tuple(locations),
        description=description,
        description_format=description_format,
        canonical_url=job_url,
        apply_url=_text(job.get("applyUrl")) or job_url,
        published_at=parse_datetime(job.get("publishedAt")),
        employment_types=employment_types,
        remote=remote,
        salary=_ashby_salary(job.get("compensation")),
        metadata={
            "department": job.get("department"),
            "team": job.get("team"),
        },
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else url


def _ashby_salary(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _text(
        value.get("scrapeableCompensationSalarySummary") or value.get("compensationTierSummary")
    )
