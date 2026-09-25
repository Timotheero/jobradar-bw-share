"""Connector for the public Oracle Recruiting Cloud candidate-experience API."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

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

_ORACLE_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+oraclecloud\.com$")
_SITE_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class OracleRecruitingCloudConnector:
    """Read an employer's unauthenticated Oracle Candidate Experience jobs."""

    MAX_PAGE_SIZE = 100
    MAX_PAGES = 100
    RESOURCE_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    source_info = SourceInfo(
        key="oracle",
        name="Oracle Recruiting Cloud",
        acquisition="official_public_api",
        official=True,
        documentation_url="https://docs.oracle.com/en/cloud/saas/human-resources/",
    )

    def __init__(
        self,
        base_url: str,
        *,
        site_number: str | int | None = None,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.base_url = _oracle_base_url(base_url)
        if site_number is None:
            self.site_number = None
        else:
            normalized_site = str(site_number).strip()
            if not _SITE_NUMBER.fullmatch(normalized_site):
                raise ValueError("Oracle site_number contains unsupported characters")
            self.site_number = normalized_site
        self.company = _optional_text(company)
        self._http = HTTPTransport(
            self.base_url,
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
        offset = (query.pagination.start_page - 1) * page_size
        requested_pages = query.pagination.max_pages or self.MAX_PAGES
        page_cap = min(requested_pages, self.MAX_PAGES)
        seen_ids: set[str] = set()

        for _ in range(page_cap):
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": offset,
                "onlyData": "true",
            }
            if self.site_number is not None:
                params["finder"] = f"findReqs;siteNumber={self.site_number}"
            payload = self._http.request_json("GET", self.RESOURCE_PATH, params=params)
            jobs, total = _oracle_page(payload)
            if not jobs:
                return

            new_jobs = 0
            for job in jobs:
                record = _normalize_oracle(self.base_url, self.company, job)
                if record is None or record.source_job_id in seen_ids:
                    continue
                seen_ids.add(record.source_job_id)
                new_jobs += 1
                if local_query_match(record, query):
                    yield record

            if new_jobs == 0:
                return
            offset += len(jobs)
            if len(jobs) < page_size or (total is not None and offset >= total):
                return


def _oracle_base_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Oracle base_url is not a valid URL") from exc
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not _ORACLE_HOST.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Oracle base_url must be an HTTPS oraclecloud.com host")
    if parsed.path not in ("", "/"):
        raise ValueError("Oracle base_url must not contain a path")
    return urlunsplit(("https", hostname, "", "", ""))


def _oracle_page(payload: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(payload, Mapping):
        raise ValueError("Oracle response must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Oracle response does not contain an items list")

    jobs: list[Mapping[str, Any]] = []
    total = _integer(payload.get("TotalJobsCount"))
    for item in items:
        if not isinstance(item, Mapping):
            continue
        requisitions = item.get("requisitionList")
        if requisitions is None:
            # Some releases return the requisitions directly in ``items``.
            if _job_identity(item) is not None:
                jobs.append(item)
            continue
        if not isinstance(requisitions, list):
            raise ValueError("Oracle requisitionList must be a list")
        jobs.extend(job for job in requisitions if isinstance(job, Mapping))
        item_total = _integer(item.get("TotalJobsCount"))
        if item_total is not None:
            total = item_total
    return jobs, total


def _normalize_oracle(
    base_url: str,
    configured_company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _job_identity(job)
    title = _first_text(job, "Title", "title", "JobTitle", "jobTitle")
    if job_id is None or title is None:
        return None

    location = _first_text(
        job,
        "PrimaryLocation",
        "primaryLocation",
        "Location",
        "location",
    )
    other_locations = string_tuple(
        job.get("OtherWorkLocations") or job.get("otherWorkLocations")
    )
    locations = tuple(dict.fromkeys(((location,) if location else ()) + other_locations))
    description = _first_text(
        job,
        "ExternalDescriptionStr",
        "externalDescriptionStr",
        "JobDescription",
        "jobDescription",
        "ShortDescriptionStr",
        "shortDescriptionStr",
    )
    canonical_url = _first_text(
        job,
        "JobDetailUrl",
        "jobDetailUrl",
        "ExternalUrl",
        "externalUrl",
        "JobUrl",
        "jobUrl",
    )
    canonical_url = _same_oracle_host_url(base_url, canonical_url)
    apply_url = _same_oracle_host_url(
        base_url,
        _first_text(job, "ApplyUrl", "applyUrl", "CandidateApplyUrl", "candidateApplyUrl"),
    ) or canonical_url
    remote_text = _first_text(job, "WorkplaceType", "workplaceType", "RemoteType", "remoteType")
    remote = None
    if remote_text:
        folded = remote_text.casefold()
        if any(value in folded for value in ("remote", "home office", "homeoffice")):
            remote = True
        elif any(value in folded for value in ("onsite", "on-site", "office")):
            remote = False

    return RawJobRecord(
        source="oracle",
        source_job_id=job_id,
        title=title,
        company=configured_company
        or _first_text(job, "LegalEmployerName", "CompanyName", "company"),
        locations=locations,
        description=description,
        description_format=(
            "html" if description and "<" in description else "text" if description else None
        ),
        canonical_url=canonical_url,
        apply_url=apply_url,
        published_at=parse_datetime(
            job.get("PostedDate") or job.get("postedDate") or job.get("PublicationDate")
        ),
        updated_at=parse_datetime(job.get("LastUpdateDate") or job.get("lastUpdateDate")),
        employment_types=string_tuple(
            job.get("WorkerType")
            or job.get("workerType")
            or job.get("JobType")
            or job.get("jobType")
        ),
        remote=remote,
        metadata={
            "requisition_number": _first_text(job, "RequisitionNumber", "requisitionNumber"),
            "job_code": _first_text(job, "JobCode", "jobCode"),
            "department": _first_text(job, "DepartmentName", "departmentName"),
            "workplace_type": remote_text,
        },
        raw=dict(job),
    )


def _same_oracle_host_url(base_url: str, value: str | None) -> str | None:
    if value is None:
        return None
    candidate = urljoin(base_url + "/", value)
    parsed = urlsplit(candidate)
    base = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        return None
    return candidate


def _job_identity(job: Mapping[str, Any]) -> str | None:
    return _first_text(
        job,
        "Id",
        "id",
        "RequisitionId",
        "requisitionId",
        "RequisitionNumber",
        "requisitionNumber",
        "JobId",
        "jobId",
    )


def _first_text(values: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _optional_text(values.get(key))
        if text is not None:
            return text
    return None


def _optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
