"""Bounded maintenance for stale jobs and retained raw source HTML."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import ApplicationStatus, JobAvailabilityStatus, JobPosting, JobSnapshot, utcnow
from .availability import JobAvailabilityChecker, check_job, external_check_allowed

DEFAULT_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    jobs_checked: int = 0
    jobs_inactivated: int = 0
    jobs_reactivated: int = 0
    checks_failed: int = 0
    raw_snapshots_cleaned: int = 0


def run_job_maintenance(
    session: Session,
    settings: Settings,
    checker: JobAvailabilityChecker,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> MaintenanceResult:
    """Check a small stale-job batch and clear expired compressed source HTML."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    checked_at = now or utcnow()
    stale_before = checked_at - timedelta(days=max(1, settings.job_inactive_after_days))
    candidates = list(
        session.scalars(
            select(JobPosting)
            .where(JobPosting.last_seen_at <= stale_before)
            .options(
                selectinload(JobPosting.source),
                selectinload(JobPosting.application),
            )
            .order_by(JobPosting.last_seen_at, JobPosting.id)
            .limit(batch_size * 4)
        ).unique()
    )
    priority = {
        ApplicationStatus.PLANNED.value: 0,
        ApplicationStatus.SAVED.value: 1,
        ApplicationStatus.NEW.value: 2,
    }
    candidates.sort(
        key=lambda job: (
            priority.get(job.application_status, 3),
            job.last_seen_at,
            job.id,
        )
    )

    jobs_checked = 0
    jobs_inactivated = 0
    jobs_reactivated = 0
    checks_failed = 0
    for job in candidates:
        if jobs_checked >= batch_size:
            break
        expires_at = _as_utc(job.expires_at)
        expiry_confirms_inactive = bool(
            expires_at is not None and expires_at <= checked_at
        )
        if not expiry_confirms_inactive and not external_check_allowed(session, job, settings):
            continue
        was_active = job.is_active
        result = check_job(session, job, checker, now=checked_at, force=True)
        jobs_checked += 1
        if result.status is JobAvailabilityStatus.CHECK_FAILED:
            checks_failed += 1
        elif was_active and result.status is JobAvailabilityStatus.INACTIVE:
            jobs_inactivated += 1
        elif not was_active and result.status is JobAvailabilityStatus.ACTIVE:
            jobs_reactivated += 1

    raw_snapshots_cleaned = _clear_expired_raw_html(
        session,
        retention_days=settings.raw_html_retention_days,
        now=checked_at,
        batch_size=batch_size,
    )
    session.commit()
    return MaintenanceResult(
        jobs_checked=jobs_checked,
        jobs_inactivated=jobs_inactivated,
        jobs_reactivated=jobs_reactivated,
        checks_failed=checks_failed,
        raw_snapshots_cleaned=raw_snapshots_cleaned,
    )


def _clear_expired_raw_html(
    session: Session,
    *,
    retention_days: int,
    now: datetime,
    batch_size: int,
) -> int:
    if retention_days <= 0:
        return 0
    cutoff = now - timedelta(days=retention_days)
    snapshots = list(
        session.scalars(
            select(JobSnapshot)
            .where(
                JobSnapshot.fetched_at < cutoff,
                JobSnapshot.raw_html_compressed.is_not(None),
            )
            .order_by(JobSnapshot.fetched_at, JobSnapshot.id)
            .limit(batch_size)
        )
    )
    for snapshot in snapshots:
        snapshot.raw_html_compressed = None
    session.flush()
    return len(snapshots)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
