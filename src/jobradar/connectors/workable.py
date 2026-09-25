"""Connector for Workable's public careers widget API."""

from __future__ import annotations

import re
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

_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class WorkableJobBoardConnector:
    """Read one employer's jobs from the unauthenticated Workable widget."""

    BASE_URL = "https://apply.workable.com"

    def __init__(
        self,
        slug: str,
        *,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        normalized_slug = slug.strip().lower()
        if not _SLUG_PATTERN.fullmatch(normalized_slug):
            raise ValueError(
                "Workable slug must contain only lowercase letters, digits, and hyphens"
            )
        self.slug = normalized_slug
        self.company = company.strip() if company and company.strip() else None
        self.source_info = SourceInfo(
            key=f"workable:{self.slug}",
            name=f"Workable job board ({self.slug})",
            acquisition="public_widget_api",
            official=True,
            documentation_url="https://workable.readme.io/reference/job-board-api",
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
        payload = self._http.request_json(
            "GET",
            f"/api/v1/widget/accounts/{quote(self.slug, safe='')}",
            params={"details": "true"},
        )
        jobs, payload_company = _workable_jobs(payload)
        company = self.company or payload_company
        seen: set[str] = set()
        for job in jobs:
            record = _normalize_workable(self.source_info.key, self.slug, company, job)
            if record is None or record.source_job_id in seen:
                continue
            seen.add(record.source_job_id)
            if local_query_match(record, query):
                yield record


def _workable_jobs(payload: Any) -> tuple[list[Mapping[str, Any]], str | None]:
    if not isinstance(payload, Mapping):
        raise TypeError("Workable response must be a JSON object")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        nested = payload.get("account")
        raw_jobs = nested.get("jobs") if isinstance(nested, Mapping) else None
    if not isinstance(raw_jobs, list):
        raise TypeError("Workable response must contain a jobs array")
    account = payload.get("account")
    account_name = _text(payload.get("name"))
    if account_name is None and isinstance(account, Mapping):
        account_name = _text(account.get("name"))
    return [item for item in raw_jobs if isinstance(item, Mapping)], account_name


def _normalize_workable(
    source: str,
    slug: str,
    company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _text(job.get("shortcode")) or _text(job.get("id"))
    title = _text(job.get("title"))
    if not job_id or not title:
        return None

    location_data = job.get("location")
    location = None
    if isinstance(location_data, Mapping):
        location = _text(location_data.get("location_str")) or _text(location_data.get("name"))
        if location is None:
            location = ", ".join(
                value
                for value in (
                    _text(location_data.get("city")),
                    _text(location_data.get("region")),
                    _text(location_data.get("country")),
                )
                if value
            ) or None
    else:
        location = _text(location_data)

    url = _text(job.get("url")) or _text(job.get("shortlink"))
    if url is None:
        url = f"https://apply.workable.com/{slug}/j/{quote(job_id, safe='')}/"
    description = _text(job.get("description")) or _text(job.get("description_html"))
    remote_value = job.get("remote")
    remote = remote_value if isinstance(remote_value, bool) else None
    if remote is None and isinstance(location_data, Mapping):
        workplace = _text(location_data.get("workplace_type"))
        if workplace:
            remote = (
                True
                if workplace.casefold() == "remote"
                else False
                if workplace.casefold() in {"on-site", "onsite"}
                else None
            )

    department = job.get("department")
    department_name = (
        _text(department.get("name"))
        if isinstance(department, Mapping)
        else _text(department)
    )
    employment = job.get("employment_type") or job.get("type")
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=string_tuple(location),
        description=description,
        description_format="html" if description else None,
        canonical_url=url,
        apply_url=_text(job.get("application_url")) or url,
        published_at=parse_datetime(job.get("published_on") or job.get("created_at")),
        updated_at=parse_datetime(job.get("updated_at")),
        employment_types=string_tuple(employment),
        remote=remote,
        metadata={"department": department_name},
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
