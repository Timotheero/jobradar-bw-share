"""Bounded, explicit checks that a stored job is still open."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..connectors.public_url import PreparedPublicURL, prepare_public_url
from ..models import JobAvailabilityStatus, JobPosting, utcnow
from .settings import database_crawling_switch, database_firecrawl_switch

DEFAULT_CACHE_AGE = timedelta(hours=6)
MAX_RESPONSE_BYTES = 2_000_000
_FIRECRAWL_SOURCE = "firecrawl_self_hosted"
_INACTIVE_MARKERS = (
    "job is no longer available",
    "job is no longer accepting applications",
    "position has been filled",
    "vacancy has expired",
    "job posting has expired",
    "stelle ist nicht mehr verfügbar",
    "stelle nicht mehr verfügbar",
    "stellenangebot ist nicht mehr verfügbar",
    "bewerbungen werden nicht mehr angenommen",
    "position wurde besetzt",
)


@dataclass(frozen=True, slots=True)
class AvailabilityCheck:
    status: JobAvailabilityStatus
    method: str
    reason: str


@dataclass(frozen=True, slots=True)
class AvailabilityConcern:
    job_id: int
    title: str
    employer: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class AvailabilitySelection:
    eligible_jobs: tuple[JobPosting, ...]
    inactive: tuple[AvailabilityConcern, ...]
    unverified: tuple[AvailabilityConcern, ...]


class JobAvailabilityChecker(Protocol):
    def check(self, job: JobPosting) -> AvailabilityCheck: ...

    def close(self) -> None: ...


class HTTPJobAvailabilityChecker:
    """Check a canonical public job page without trusting redirects or generic HTTP 200s."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        url_preparer=prepare_public_url,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
            headers={"User-Agent": "Jobradar/0.1"},
        )
        self._owns_client = client is None
        self._url_preparer = url_preparer

    def check(self, job: JobPosting) -> AvailabilityCheck:
        if not job.canonical_url:
            return AvailabilityCheck(
                JobAvailabilityStatus.CHECK_FAILED,
                "canonical_url_http",
                "missing_canonical_url",
            )
        try:
            prepared: PreparedPublicURL = self._url_preparer(job.canonical_url)
            with self._client.stream("GET", prepared.final_url) as response:
                status_code = response.status_code
                if status_code in {404, 410}:
                    return AvailabilityCheck(
                        JobAvailabilityStatus.INACTIVE,
                        "canonical_url_http",
                        f"http_{status_code}",
                    )
                if status_code < 200 or status_code >= 300:
                    return AvailabilityCheck(
                        JobAvailabilityStatus.CHECK_FAILED,
                        "canonical_url_http",
                        "unexpected_http_status",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        return AvailabilityCheck(
                            JobAvailabilityStatus.CHECK_FAILED,
                            "canonical_url_http",
                            "response_too_large",
                        )
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, ValueError):
            return AvailabilityCheck(
                JobAvailabilityStatus.CHECK_FAILED,
                "canonical_url_http",
                "request_failed",
            )
        return _classify_page(job, text)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HTTPJobAvailabilityChecker:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def check_job(
    session: Session,
    job: JobPosting,
    checker: JobAvailabilityChecker,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_CACHE_AGE,
    force: bool = False,
) -> AvailabilityCheck:
    """Check one job, cache the result, and never deactivate it for a transient failure."""

    checked_at = now or utcnow()
    expires_at = _as_utc(job.expires_at)
    if expires_at is not None and expires_at <= checked_at:
        result = AvailabilityCheck(
            JobAvailabilityStatus.INACTIVE,
            "published_expiry",
            "published_expiry_passed",
        )
    elif not force and _is_fresh(job.availability_checked_at, checked_at, max_age):
        return AvailabilityCheck(
            JobAvailabilityStatus(job.availability_status),
            job.availability_check_method or "cached",
            job.availability_reason or "cached_result",
        )
    else:
        result = checker.check(job)

    job.availability_status = result.status.value
    job.availability_checked_at = checked_at
    job.availability_check_method = result.method
    job.availability_reason = result.reason
    if result.status is JobAvailabilityStatus.ACTIVE:
        job.is_active = True
        job.availability_failures = 0
    elif result.status is JobAvailabilityStatus.INACTIVE:
        job.is_active = False
        job.availability_failures = 0
    else:
        job.availability_failures += 1
    session.flush()
    return result


def record_unverified_check(
    session: Session,
    job: JobPosting,
    *,
    reason: str,
    now: datetime | None = None,
) -> AvailabilityCheck:
    result = AvailabilityCheck(JobAvailabilityStatus.CHECK_FAILED, "access_gate", reason)
    job.availability_status = result.status.value
    job.availability_checked_at = now or utcnow()
    job.availability_check_method = result.method
    job.availability_reason = result.reason
    job.availability_failures += 1
    session.flush()
    return result


def external_check_allowed(
    session: Session,
    job: JobPosting,
    settings: Settings,
) -> bool:
    api_access = settings.crawling_enabled and database_crawling_switch(session)
    source_slug = job.source.slug if job.source is not None else ""
    if source_slug != _FIRECRAWL_SOURCE:
        return api_access
    return api_access and settings.firecrawl_enabled and database_firecrawl_switch(session)


def verify_jobs_for_application(
    session: Session,
    jobs: Iterable[JobPosting],
    checker: JobAvailabilityChecker,
    settings: Settings,
    *,
    allow_unverified: bool = False,
    now: datetime | None = None,
) -> AvailabilitySelection:
    """Return only jobs safe to continue, with concerns for a confirmation screen."""

    eligible: list[JobPosting] = []
    inactive: list[AvailabilityConcern] = []
    unverified: list[AvailabilityConcern] = []
    checked_at = now or utcnow()
    for job in jobs:
        source_slug = job.source.slug if job.source is not None else ""
        access_allowed = external_check_allowed(session, job, settings)
        result = (
            check_job(session, job, checker, now=checked_at)
            if access_allowed
            else record_unverified_check(
                session,
                job,
                reason=(
                    "firecrawl_access_disabled"
                    if source_slug == _FIRECRAWL_SOURCE
                    else "source_access_disabled"
                ),
                now=checked_at,
            )
        )
        concern = AvailabilityConcern(job.id, job.title, job.employer, result.reason)
        if result.status is JobAvailabilityStatus.INACTIVE:
            inactive.append(concern)
        elif result.status is JobAvailabilityStatus.CHECK_FAILED:
            unverified.append(concern)
            if allow_unverified:
                eligible.append(job)
        else:
            eligible.append(job)
    return AvailabilitySelection(tuple(eligible), tuple(inactive), tuple(unverified))


def _classify_page(job: JobPosting, raw_html: str) -> AvailabilityCheck:
    compact_html = re.sub(r"\s+", " ", raw_html).casefold()
    visible_text = unescape(re.sub(r"<[^>]+>", " ", compact_html))
    visible_text = re.sub(r"\s+", " ", visible_text)
    if any(marker in visible_text for marker in _INACTIVE_MARKERS):
        return AvailabilityCheck(
            JobAvailabilityStatus.INACTIVE,
            "canonical_url_http",
            "closed_page_marker",
        )
    if re.search(r'["\']@type["\']\s*:\s*["\']jobposting["\']', compact_html):
        return AvailabilityCheck(
            JobAvailabilityStatus.ACTIVE,
            "canonical_url_http",
            "jobposting_structured_data",
        )
    normalized_title = re.sub(r"\s+", " ", unescape(job.title)).strip().casefold()
    if len(normalized_title) >= 8 and normalized_title in visible_text:
        return AvailabilityCheck(
            JobAvailabilityStatus.ACTIVE,
            "canonical_url_http",
            "job_title_present",
        )
    return AvailabilityCheck(
        JobAvailabilityStatus.CHECK_FAILED,
        "canonical_url_http",
        "job_content_not_recognized",
    )


def _is_fresh(value: datetime | None, now: datetime, max_age: timedelta) -> bool:
    checked_at = _as_utc(value)
    return checked_at is not None and checked_at >= now - max_age


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
