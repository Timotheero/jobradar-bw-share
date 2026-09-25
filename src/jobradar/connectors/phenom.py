"""Connector for public employer-branded Phenom career-site widgets."""

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

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class PhenomConnector:
    """Read the no-auth ``refineSearch`` widget of a Phenom career site."""

    MAX_PAGE_SIZE = 100
    MAX_PAGES = 100

    source_info = SourceInfo(
        key="phenom",
        name="Phenom career site",
        acquisition="public_widget_api",
        official=True,
        notes="Employer-branded public refineSearch widget endpoint.",
    )

    def __init__(
        self,
        careers_url: str,
        *,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
        widget_path: str = "/widgets",
    ) -> None:
        self.careers_url = _careers_url(careers_url)
        self.company = _optional_text(company)
        self.widget_path = _widget_path(widget_path)
        parsed = urlsplit(self.careers_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self._http = HTTPTransport(
            origin,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": origin,
                "Referer": self.careers_url,
            },
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
            body = {
                "ddoKey": "refineSearch",
                "from": offset,
                "size": page_size,
                "keyword": query.text or "",
                "location": query.location or "",
            }
            payload = self._http.request_json(
                "POST",
                self.widget_path,
                json=body,
                retryable=True,
            )
            jobs, total = _phenom_page(payload)
            if not jobs:
                return

            new_jobs = 0
            for job in jobs:
                record = _normalize_phenom(self.careers_url, self.company, job)
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


def _careers_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Phenom careers_url is not a valid URL") from exc
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    labels = hostname.split(".")
    if (
        parsed.scheme.casefold() != "https"
        or len(labels) < 2
        or any(not _HOST_LABEL.fullmatch(label) for label in labels)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Phenom careers_url must be a public HTTPS career-site URL")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".lan")):
        raise ValueError("Phenom careers_url host must be public")
    # Phenom deliberately serves employer-owned branded domains, so validation
    # cannot require a phenompeople.com suffix. A plausible career-site host is
    # nevertheless mandatory to prevent accepting an arbitrary API target.
    first_label = labels[0]
    phenom_host = hostname == "phenompeople.com" or hostname.endswith(".phenompeople.com")
    branded_host = first_label in {"career", "careers", "job", "jobs"}
    if not phenom_host and not branded_host:
        raise ValueError(
            "Phenom careers_url host must be phenompeople.com or a career/jobs subdomain"
        )
    path = parsed.path or "/"
    return urlunsplit(("https", hostname, path, "", ""))


def _widget_path(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        not candidate.startswith("/")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or ".." in parsed.path.split("/")
    ):
        raise ValueError("Phenom widget_path must be an absolute path on the careers host")
    return parsed.path


def _phenom_page(payload: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(payload, Mapping):
        raise ValueError("Phenom response must be a JSON object")
    root = payload.get("refineSearch")
    if root is None:
        root = payload.get("data", payload)
    if isinstance(root, Mapping) and isinstance(root.get("refineSearch"), Mapping):
        root = root["refineSearch"]
    if not isinstance(root, Mapping):
        raise ValueError("Phenom response does not contain refineSearch data")

    jobs: Any = None
    for key in ("jobs", "jobResults", "results", "hits"):
        candidate = root.get(key)
        if isinstance(candidate, list):
            jobs = candidate
            break
        if isinstance(candidate, Mapping) and isinstance(candidate.get("hits"), list):
            jobs = candidate["hits"]
            break
    if not isinstance(jobs, list):
        raise ValueError("Phenom refineSearch response does not contain a jobs list")

    normalized: list[Mapping[str, Any]] = []
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        source = job.get("_source")
        normalized.append(source if isinstance(source, Mapping) else job)

    total = None
    for key in ("total", "totalCount", "totalHits", "count", "jobCount"):
        candidate = root.get(key)
        if isinstance(candidate, Mapping):
            candidate = candidate.get("value")
        total = _integer(candidate)
        if total is not None:
            break
    return normalized, total


def _normalize_phenom(
    careers_url: str,
    configured_company: str | None,
    job: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _first_text(
        job,
        "jobId",
        "jobID",
        "id",
        "reqId",
        "requisitionId",
        "requisitionNumber",
    )
    title = _first_text(job, "title", "jobTitle", "name")
    if job_id is None or title is None:
        return None

    locations = _locations(job)
    description = _first_text(
        job,
        "description",
        "jobDescription",
        "descriptionHtml",
        "jobDescriptionText",
    )
    canonical_url = _career_url(
        careers_url,
        _first_text(job, "jobUrl", "url", "jobDetailUrl", "externalPath", "seoUrl"),
    )
    apply_url = _career_url(
        careers_url,
        _first_text(job, "applyUrl", "applyURL", "applicationUrl"),
    ) or canonical_url
    remote_value = job.get("remote")
    remote: bool | None = remote_value if isinstance(remote_value, bool) else None
    workplace = _first_text(job, "workplaceType", "workLocationType", "remoteType")
    if remote is None and workplace:
        folded = workplace.casefold()
        if any(term in folded for term in ("remote", "home office", "homeoffice")):
            remote = True
        elif any(term in folded for term in ("onsite", "on-site", "office")):
            remote = False

    return RawJobRecord(
        source="phenom",
        source_job_id=job_id,
        title=title,
        company=configured_company or _first_text(job, "company", "companyName", "brand"),
        locations=locations,
        description=description,
        description_format=(
            "html" if description and "<" in description else "text" if description else None
        ),
        canonical_url=canonical_url,
        apply_url=apply_url,
        published_at=parse_datetime(
            job.get("postedDate")
            or job.get("datePosted")
            or job.get("publicationDate")
            or job.get("createdDate")
        ),
        updated_at=parse_datetime(job.get("updatedDate") or job.get("lastModifiedDate")),
        employment_types=string_tuple(
            job.get("employmentType") or job.get("jobType") or job.get("schedule")
        ),
        remote=remote,
        metadata={
            "requisition_number": _first_text(job, "requisitionNumber", "reqId"),
            "category": _first_text(job, "category", "jobCategory"),
            "department": _first_text(job, "department", "function"),
            "workplace_type": workplace,
        },
        raw=dict(job),
    )


def _locations(job: Mapping[str, Any]) -> tuple[str, ...]:
    values = job.get("locations") or job.get("location") or job.get("jobLocation")
    result = list(string_tuple(values))
    if not result:
        rendered = ", ".join(
            part
            for part in (
                _first_text(job, "city"),
                _first_text(job, "state", "region"),
                _first_text(job, "country"),
            )
            if part
        )
        if rendered:
            result.append(rendered)
    return tuple(result)


def _career_url(careers_url: str, value: str | None) -> str | None:
    if value is None:
        return None
    candidate = urljoin(careers_url, value)
    parsed = urlsplit(candidate)
    base = urlsplit(careers_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        return None
    return candidate


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
