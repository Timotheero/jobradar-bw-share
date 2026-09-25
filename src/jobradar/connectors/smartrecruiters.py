"""Connector for the public SmartRecruiters Job Board API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, local_query_match, parse_datetime
from .http import HTTPTransport, RetryPolicy, TimeoutOptions


class SmartRecruitersJobBoardConnector:
    BASE_URL = "https://api.smartrecruiters.com/v1"
    MAX_PAGE_SIZE = 100

    def __init__(
        self,
        company_identifier: str,
        *,
        company: str | None = None,
        include_details: bool = True,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not company_identifier.strip():
            raise ValueError("SmartRecruiters company_identifier must not be empty")
        self.company_identifier = company_identifier.strip()
        self.company = company
        self.include_details = include_details
        self.source_info = SourceInfo(
            key=f"smartrecruiters:{self.company_identifier}",
            name=f"SmartRecruiters job board ({self.company_identifier})",
            acquisition="official_public_api",
            official=True,
            documentation_url=(
                "https://developers.smartrecruiters.com/reference/get-all-postings"
            ),
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
        offset = max(0, query.pagination.start_page - 1) * page_size
        pages_read = 0
        company_path = quote(self.company_identifier, safe="")

        while query.pagination.max_pages is None or pages_read < query.pagination.max_pages:
            payload = self._http.request_json(
                "GET",
                f"/companies/{company_path}/postings",
                params={"limit": page_size, "offset": offset},
            )
            if not isinstance(payload, Mapping):
                raise TypeError("SmartRecruiters response must be a JSON object")
            summaries = payload.get("content")
            if not isinstance(summaries, list) or not summaries:
                return

            for summary in summaries:
                if not isinstance(summary, Mapping):
                    continue
                detail = self._posting_detail(company_path, summary)
                record = _normalize_smartrecruiters(
                    self.source_info.key,
                    self.company,
                    self.company_identifier,
                    detail,
                )
                if record is not None and local_query_match(record, query):
                    yield record

            pages_read += 1
            offset += len(summaries)
            total = _integer(payload.get("totalFound"))
            if len(summaries) < page_size or (total is not None and offset >= total):
                return

    def _posting_detail(
        self, company_path: str, summary: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        posting_id = _text(summary.get("id"))
        if not self.include_details or not posting_id:
            return summary
        try:
            detail = self._http.request_json(
                "GET",
                f"/companies/{company_path}/postings/{quote(posting_id, safe='')}",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return summary
            raise
        return detail if isinstance(detail, Mapping) else summary


def _normalize_smartrecruiters(
    source: str,
    configured_company: str | None,
    company_identifier: str,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _text(job.get("id")) or _text(job.get("uuid"))
    title = _text(job.get("name"))
    if not job_id or not title:
        return None

    company_data = job.get("company")
    company = configured_company
    if company is None and isinstance(company_data, Mapping):
        company = _text(company_data.get("name"))

    location_data = job.get("location")
    locations: tuple[str, ...] = ()
    remote: bool | None = None
    country: str | None = None
    remote_type: str | None = None
    if isinstance(location_data, Mapping):
        rendered_location = _text(location_data.get("fullLocation"))
        if rendered_location is None:
            rendered_location = ", ".join(
                part
                for part in (
                    _text(location_data.get("city")),
                    _text(location_data.get("region")),
                    _text(location_data.get("country")),
                )
                if part
            ) or None
        if rendered_location:
            locations = (rendered_location,)
        if isinstance(location_data.get("remote"), bool):
            remote = location_data["remote"]
        country = _text(location_data.get("country"))
        if location_data.get("hybrid") is True:
            remote_type = "hybrid"
        elif remote is True:
            remote_type = "remote"
        elif remote is False:
            remote_type = "onsite"

    sections: list[str] = []
    job_ad = job.get("jobAd")
    if isinstance(job_ad, Mapping):
        raw_sections = job_ad.get("sections")
        if isinstance(raw_sections, Mapping):
            for section in raw_sections.values():
                if not isinstance(section, Mapping):
                    continue
                heading = _text(section.get("title"))
                text = _text(section.get("text"))
                if text:
                    sections.append(f"<h2>{heading}</h2>{text}" if heading else text)
    description = "\n".join(sections) or None

    employment = job.get("typeOfEmployment")
    employment_type = (
        _text(employment.get("label")) if isinstance(employment, Mapping) else None
    )
    department = job.get("department")
    function = job.get("function")
    language = job.get("language")
    posting_url = _text(job.get("postingUrl"))
    if posting_url is None:
        posting_url = (
            f"https://jobs.smartrecruiters.com/{company_identifier}/{job_id}"
        )

    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=locations,
        description=description,
        description_format="html" if description else None,
        canonical_url=posting_url,
        apply_url=_text(job.get("applyUrl")) or posting_url,
        published_at=parse_datetime(job.get("releasedDate")),
        employment_types=(employment_type,) if employment_type else (),
        remote=remote,
        metadata={
            "country": country,
            "remote_type": remote_type,
            "department": (
                _text(department.get("label")) if isinstance(department, Mapping) else None
            ),
            "function": (
                _text(function.get("label")) if isinstance(function, Mapping) else None
            ),
            "language": (
                _text(language.get("code")) if isinstance(language, Mapping) else None
            ),
            "reference": _text(job.get("refNumber")),
        },
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
