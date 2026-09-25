"""Connector for configured public SAP SuccessFactors career-site searches."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

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

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_JOB_LINK_PATTERN = re.compile(
    r"<a\b(?P<attrs>[^>]*)>(?P<title>.*?)</a>", re.IGNORECASE | re.DOTALL
)
_ATTR_PATTERN = re.compile(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")


class SuccessFactorsJobBoardConnector:
    """Read an explicitly configured public RMK/CSB JSON or HTML search path."""

    MAX_PAGE_SIZE = 100
    DEFAULT_MAX_PAGES = 100

    def __init__(
        self,
        careers_url: str,
        *,
        identifier: str,
        company: str | None = None,
        search_path: str = "/search/",
        response_format: Literal["json", "html"] = "json",
        keyword_parameter: str = "q",
        location_parameter: str = "locationsearch",
        offset_parameter: str = "startrow",
        page_size_parameter: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self.base_url = _validated_careers_url(careers_url)
        self.identifier = _identifier(identifier)
        self.search_path = _validated_path(search_path)
        if response_format not in {"json", "html"}:
            raise ValueError("response_format must be 'json' or 'html'")
        self.response_format = response_format
        self.keyword_parameter = _parameter_name(keyword_parameter)
        self.location_parameter = _parameter_name(location_parameter)
        self.offset_parameter = _parameter_name(offset_parameter)
        self.page_size_parameter = (
            _parameter_name(page_size_parameter) if page_size_parameter is not None else None
        )
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self.max_pages = max_pages
        self.company = company.strip() if company and company.strip() else None
        self.source_info = SourceInfo(
            key=f"successfactors:{self.identifier}",
            name=f"SuccessFactors job board ({self.identifier})",
            acquisition="public_career_site",
            official=True,
        )
        accept = "application/json" if response_format == "json" else "text/html"
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": accept},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page_size = min(query.pagination.page_size, self.MAX_PAGE_SIZE)
        offset = (query.pagination.start_page - 1) * page_size
        page_limit = min(query.pagination.max_pages or self.max_pages, self.max_pages)
        seen: set[str] = set()

        for _ in range(page_limit):
            params: dict[str, Any] = {
                self.keyword_parameter: query.text or "",
                self.location_parameter: query.location or "",
                self.offset_parameter: offset,
            }
            if self.page_size_parameter:
                params[self.page_size_parameter] = page_size
            params.update(query.filters)
            response = self._http.request("GET", self.search_path, params=params)
            if self.response_format == "json":
                jobs, total = _successfactors_jobs(response.json())
            else:
                jobs, total = _successfactors_html(response.text, self.base_url)
            if not jobs:
                return
            unseen = 0
            for job in jobs:
                record = _normalize_successfactors(
                    self.source_info.key, self.base_url, self.company, job
                )
                if record is None or record.source_job_id in seen:
                    continue
                seen.add(record.source_job_id)
                unseen += 1
                if local_query_match(record, query):
                    yield record
            offset += len(jobs)
            if unseen == 0 or len(jobs) < page_size or (total is not None and offset >= total):
                return


def _validated_careers_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("SuccessFactors careers_url must be HTTPS and contain no credentials")
    if parsed.port not in (None, 443) or parsed.query or parsed.fragment:
        raise ValueError(
            "SuccessFactors careers_url must use port 443 and contain no query or fragment"
        )
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or "." not in hostname:
        raise ValueError("SuccessFactors careers_url must use a fully-qualified public host")
    labels = hostname.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("SuccessFactors careers_url contains an invalid host")
    return f"https://{hostname}{parsed.path.rstrip('/')}"


def _identifier(value: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError("SuccessFactors identifier contains unsupported characters")
    return normalized


def _validated_path(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("SuccessFactors search_path must be an absolute URL path")
    return parsed.path


def _parameter_name(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", normalized):
        raise ValueError("SuccessFactors parameter names contain unsupported characters")
    return normalized


def _successfactors_jobs(payload: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(payload, Mapping):
        raise TypeError("SuccessFactors response must be a JSON object")
    container: Mapping[str, Any] = payload
    for key in ("searchResults", "data", "results"):
        candidate = container.get(key)
        if isinstance(candidate, Mapping):
            container = candidate
            break
    raw_jobs = None
    for key in ("jobs", "results", "jobPostings", "searchResults"):
        if isinstance(container.get(key), list):
            raw_jobs = container[key]
            break
    if raw_jobs is None and isinstance(payload.get("jobs"), list):
        raw_jobs = payload["jobs"]
    if not isinstance(raw_jobs, list):
        raise TypeError("SuccessFactors response must contain a jobs/results array")
    total = next(
        (
            container.get(key)
            for key in ("totalCount", "total", "totalJobs")
            if key in container
        ),
        None,
    )
    parsed_total = total if isinstance(total, int) and not isinstance(total, bool) else None
    return [item for item in raw_jobs if isinstance(item, Mapping)], parsed_total


def _successfactors_html(markup: str, base_url: str) -> tuple[list[Mapping[str, Any]], int | None]:
    jobs: list[Mapping[str, Any]] = []
    for match in _JOB_LINK_PATTERN.finditer(markup):
        attrs = {
            name.casefold(): html.unescape(value)
            for name, _, value in _ATTR_PATTERN.findall(match.group("attrs"))
        }
        href = attrs.get("href")
        if not href or "/job/" not in href.casefold():
            continue
        title = html.unescape(_TAG_PATTERN.sub(" ", match.group("title"))).strip()
        if not title:
            continue
        jobs.append(
            {
                "id": attrs.get("data-job-id") or href,
                "title": title,
                "url": urljoin(base_url + "/", href),
            }
        )
    return jobs, None


def _normalize_successfactors(
    source: str, base_url: str, company: str | None, job: Mapping[str, Any]
) -> RawJobRecord | None:
    job_id = _text(
        job.get("jobId")
        or job.get("jobReqId")
        or job.get("requisitionId")
        or job.get("id")
    )
    title = _text(job.get("jobTitle") or job.get("title") or job.get("name"))
    if not job_id or not title:
        return None
    raw_url = _text(job.get("url") or job.get("jobUrl") or job.get("canonicalUrl"))
    canonical_url = urljoin(base_url + "/", raw_url) if raw_url else None
    description = _text(job.get("jobDescription") or job.get("description"))
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company or _text(job.get("company") or job.get("companyName")),
        locations=string_tuple(
            job.get("locations") or job.get("location") or job.get("jobLocation")
        ),
        description=description,
        description_format="html" if description else None,
        canonical_url=canonical_url,
        apply_url=_text(job.get("applyUrl")) or canonical_url,
        published_at=parse_datetime(job.get("postedDate") or job.get("datePosted")),
        updated_at=parse_datetime(job.get("updatedDate") or job.get("lastModified")),
        employment_types=string_tuple(job.get("employmentType") or job.get("jobType")),
        metadata={"requisition_id": job.get("requisitionId") or job.get("jobReqId")},
        raw=dict(job),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
