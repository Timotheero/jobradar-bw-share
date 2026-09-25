"""Connector for the public Greenhouse Job Board API."""

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


class GreenhouseJobBoardConnector:
    BASE_URL = "https://boards-api.greenhouse.io/v1"

    def __init__(
        self,
        board_token: str,
        *,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not board_token.strip():
            raise ValueError("Greenhouse board_token must not be empty")
        self.board_token = board_token.strip()
        self.company = company
        self.source_info = SourceInfo(
            key=f"greenhouse:{self.board_token}",
            name=f"Greenhouse job board ({self.board_token})",
            acquisition="official_public_api",
            official=True,
            documentation_url="https://developers.greenhouse.io/job-board.html",
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
            f"/boards/{quote(self.board_token, safe='')}/jobs",
            params={"content": "true"},
        )
        if not isinstance(payload, Mapping):
            raise TypeError("Greenhouse response must be a JSON object")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            return
        company = self.company or _text(payload.get("name"))
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            record = _normalize_greenhouse(self.source_info.key, company, job)
            if record is not None and local_query_match(record, query):
                yield record


def _normalize_greenhouse(
    source: str,
    company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _text(job.get("id"))
    title = _text(job.get("title"))
    if not job_id or not title:
        return None
    locations: list[str] = []
    primary = job.get("location")
    if isinstance(primary, Mapping) and _text(primary.get("name")):
        locations.append(_text(primary.get("name")) or "")
    for office in job.get("offices") or []:
        if isinstance(office, Mapping):
            name = _text(office.get("location") or office.get("name"))
            if name and name not in locations:
                locations.append(name)

    employment_types: tuple[str, ...] = ()
    metadata = job.get("metadata")
    if isinstance(metadata, list):
        for item in metadata:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").casefold()
            if "employment" in name or "commitment" in name:
                employment_types = string_tuple(item.get("value"))
                break

    url = _text(job.get("absolute_url"))
    remote = True if any("remote" in item.casefold() for item in locations) else None
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company or _text(job.get("company_name")),
        locations=tuple(locations),
        description=_text(job.get("content")),
        description_format="html",
        canonical_url=url,
        apply_url=url,
        updated_at=parse_datetime(job.get("updated_at")),
        employment_types=employment_types,
        remote=remote,
        metadata={
            "internal_job_id": job.get("internal_job_id"),
            "requisition_id": job.get("requisition_id"),
        },
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
