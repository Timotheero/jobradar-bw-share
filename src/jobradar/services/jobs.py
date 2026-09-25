"""Job storage, retrieval, scoring, and application-state services."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..domain.scoring import SCORING_VERSION, evaluate_job
from ..models import (
    ApplicationRecord,
    ApplicationStatus,
    CandidateProfile,
    FeedbackEvent,
    JobPosting,
    JobScore,
    JobSnapshot,
    PreferenceProfile,
    utcnow,
)
from .rescore import request_rescore


@dataclass(slots=True)
class JobSearchItem:
    job: JobPosting
    score: JobScore | None


@dataclass(slots=True)
class JobSearchResult:
    items: list[JobSearchItem]
    total: int
    limit: int
    offset: int


class JobNotFoundError(LookupError):
    pass


def _normalize_key_part(value: str | None) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", (value or "").casefold()).strip()


def make_canonical_key(*, title: str, employer: str | None, location: str | None) -> str:
    basis = "|".join(_normalize_key_part(part) for part in (title, employer, location))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def make_content_hash(values: dict[str, Any]) -> str:
    stable = {
        key: values.get(key)
        for key in (
            "title",
            "employer",
            "description_text",
            "location_text",
            "remote_type",
            "employment_type",
            "contract_type",
            "published_at",
            "expires_at",
            "structured_data",
        )
    }
    encoded = json.dumps(stable, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upsert_job(
    session: Session,
    *,
    source_id: int,
    external_id: str,
    values: dict[str, Any],
    raw_html_compressed: bytes | None = None,
    commit: bool = True,
) -> tuple[JobPosting, bool, bool]:
    """Insert/update a normalized job and retain only changed snapshots.

    Returns ``(job, created, changed)``.  Cross-source duplicate candidates can
    later be grouped through ``canonical_key`` without discarding provenance.
    """

    job = session.scalar(
        select(JobPosting).where(
            JobPosting.source_id == source_id,
            JobPosting.external_id == external_id,
        )
    )
    created = job is None
    if job is None:
        if not values.get("title"):
            raise ValueError("A job title is required")
        job = JobPosting(
            source_id=source_id,
            external_id=external_id,
            title=str(values["title"]),
        )
        session.add(job)

    allowed = {
        "canonical_url",
        "canonical_key",
        "title",
        "employer",
        "description_text",
        "location_text",
        "postcode",
        "city",
        "state",
        "country",
        "remote_type",
        "employment_type",
        "contract_type",
        "language",
        "published_at",
        "expires_at",
        "is_active",
        "availability_status",
        "availability_checked_at",
        "availability_check_method",
        "availability_reason",
        "availability_failures",
        "structured_data",
    }
    normalized_values = {key: value for key, value in values.items() if key in allowed}
    new_hash = values.get("content_hash") or make_content_hash(normalized_values)
    old_hash = job.content_hash
    changed = created or old_hash != new_hash

    for key, value in normalized_values.items():
        setattr(job, key, value)
    job.canonical_key = job.canonical_key or make_canonical_key(
        title=job.title, employer=job.employer, location=job.location_text
    )
    job.content_hash = new_hash
    job.last_seen_at = utcnow()
    session.flush()

    if changed:
        existing_snapshot_id = session.scalar(
            select(JobSnapshot.id).where(
                JobSnapshot.job_id == job.id,
                JobSnapshot.content_hash == new_hash,
            )
        )
        if existing_snapshot_id is None:
            snapshot = JobSnapshot(
                job_id=job.id,
                content_hash=new_hash,
                cleaned_text=job.description_text,
                raw_html_compressed=raw_html_compressed,
                structured_data=dict(job.structured_data or {}),
                change_kind="created" if created else "changed",
            )
            session.add(snapshot)

    if commit:
        session.commit()
        session.refresh(job)
    else:
        session.flush()
    return job, created, changed


def _apply_job_filters(
    statement: Select[Any],
    *,
    query: str | None,
    active_only: bool,
    source_id: int | None,
    employment_type: str | None,
    remote_type: str | None,
    state: str | None,
    status: str | None,
    min_relevance: float | None,
) -> Select[Any]:
    if query:
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                JobPosting.title.ilike(pattern),
                JobPosting.employer.ilike(pattern),
                JobPosting.description_text.ilike(pattern),
                JobPosting.location_text.ilike(pattern),
            )
        )
    if active_only:
        statement = statement.where(JobPosting.is_active.is_(True))
    if source_id is not None:
        statement = statement.where(JobPosting.source_id == source_id)
    if employment_type:
        statement = statement.where(JobPosting.employment_type.ilike(f"%{employment_type}%"))
    if remote_type:
        statement = statement.where(JobPosting.remote_type == remote_type)
    if state:
        statement = statement.where(JobPosting.state == state)
    if status:
        if status == ApplicationStatus.NEW.value:
            statement = statement.where(
                or_(ApplicationRecord.id.is_(None), ApplicationRecord.status == status)
            )
        else:
            statement = statement.where(ApplicationRecord.status == status)
    if min_relevance is not None:
        statement = statement.where(JobScore.relevance_score >= min_relevance)
    return statement


def list_jobs(
    session: Session,
    *,
    query: str | None = None,
    active_only: bool = True,
    source_id: int | None = None,
    employment_type: str | None = None,
    remote_type: str | None = None,
    state: str | None = None,
    status: str | None = None,
    min_relevance: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JobSearchResult:
    latest_ids = (
        select(JobScore.job_id, func.max(JobScore.id).label("score_id"))
        .group_by(JobScore.job_id)
        .subquery()
    )
    joins = (
        select(JobPosting, JobScore)
        .outerjoin(latest_ids, latest_ids.c.job_id == JobPosting.id)
        .outerjoin(JobScore, JobScore.id == latest_ids.c.score_id)
        .outerjoin(ApplicationRecord, ApplicationRecord.job_id == JobPosting.id)
    )
    joins = _apply_job_filters(
        joins,
        query=query,
        active_only=active_only,
        source_id=source_id,
        employment_type=employment_type,
        remote_type=remote_type,
        state=state,
        status=status,
        min_relevance=min_relevance,
    )
    total_statement = joins.with_only_columns(func.count(JobPosting.id)).order_by(None)
    total = int(session.scalar(total_statement) or 0)

    statement = (
        joins.options(
            selectinload(JobPosting.source),
            selectinload(JobPosting.application),
        )
        .order_by(
            JobScore.relevance_score.desc().nullslast(),
            JobScore.candidate_fit_score.desc().nullslast(),
            JobPosting.published_at.desc().nullslast(),
            JobPosting.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    rows = session.execute(statement).all()
    return JobSearchResult(
        items=[JobSearchItem(job=row[0], score=row[1]) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_job(session: Session, job_id: int) -> JobPosting | None:
    return session.scalar(
        select(JobPosting)
        .where(JobPosting.id == job_id)
        .options(
            selectinload(JobPosting.source),
            selectinload(JobPosting.snapshots),
            selectinload(JobPosting.scores),
            selectinload(JobPosting.application),
        )
    )


def require_job(session: Session, job_id: int) -> JobPosting:
    job = get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} does not exist")
    return job


def score_job(
    session: Session,
    job: JobPosting,
    *,
    candidate: CandidateProfile | None = None,
    preferences: PreferenceProfile | None = None,
    commit: bool = True,
) -> JobScore:
    relevance, candidate_fit = evaluate_job(job, candidate, preferences)
    row = JobScore(
        job_id=job.id,
        candidate_profile_id=candidate.id if candidate else None,
        profile_version=candidate.version if candidate else None,
        preference_version=preferences.version if preferences else None,
        relevance_score=relevance.value,
        candidate_fit_score=candidate_fit.value,
        relevance_reasons=relevance.reasons_as_dicts(),
        fit_reasons=candidate_fit.reasons_as_dicts(),
        scoring_version=SCORING_VERSION,
    )
    session.add(row)
    if commit:
        session.commit()
        session.refresh(row)
    else:
        session.flush()
    return row


def update_application_status(
    session: Session,
    job_id: int,
    status: ApplicationStatus | str,
    *,
    notes: str | None = None,
) -> ApplicationRecord:
    require_job(session, job_id)
    status_value = status.value if isinstance(status, ApplicationStatus) else status
    allowed = {item.value for item in ApplicationStatus}
    if status_value not in allowed:
        raise ValueError(f"Unknown application status: {status_value}")

    record = session.scalar(select(ApplicationRecord).where(ApplicationRecord.job_id == job_id))
    if record is None:
        record = ApplicationRecord(job_id=job_id)
        session.add(record)
    old_status = record.status
    record.status = status_value
    if notes is not None:
        record.notes = notes

    now = utcnow()
    timestamp_fields = {
        ApplicationStatus.PLANNED.value: "planned_at",
        ApplicationStatus.APPLIED.value: "applied_at",
        ApplicationStatus.INTERVIEW.value: "interview_at",
        ApplicationStatus.OFFER.value: "offer_at",
        ApplicationStatus.CLOSED.value: "closed_at",
    }
    timestamp_field = timestamp_fields.get(status_value)
    if timestamp_field and getattr(record, timestamp_field) is None:
        setattr(record, timestamp_field, now)

    if old_status != status_value and status_value in {
        ApplicationStatus.SAVED.value,
        ApplicationStatus.NOT_SUITABLE.value,
    }:
        positive = status_value == ApplicationStatus.SAVED.value
        session.add(
            FeedbackEvent(
                job_id=job_id,
                event_type="application_status",
                value=status_value,
                weight_delta={"direction": 1 if positive else -1, "magnitude": 1},
                explanation=(
                    "Merken wird als positives Präferenzsignal gespeichert."
                    if positive
                    else "Ablehnen wird als negatives Präferenzsignal gespeichert."
                ),
            )
        )
        request_rescore(
            session,
            reason="feedback_changed",
            commit=False,
        )
    session.commit()
    session.refresh(record)
    return record


def record_feedback(
    session: Session,
    job_id: int,
    *,
    event_type: str,
    value: str | None = None,
    weight_delta: dict[str, Any] | None = None,
    explanation: str | None = None,
) -> FeedbackEvent:
    require_job(session, job_id)
    event = FeedbackEvent(
        job_id=job_id,
        event_type=event_type,
        value=value,
        weight_delta=weight_delta or {},
        explanation=explanation,
    )
    session.add(event)
    if event_type == "application_status" and value in {
        ApplicationStatus.SAVED.value,
        ApplicationStatus.NOT_SUITABLE.value,
    }:
        request_rescore(session, reason="feedback_changed", commit=False)
    session.commit()
    session.refresh(event)
    return event


def latest_scored_at(session: Session) -> datetime | None:
    return session.scalar(select(func.max(JobScore.created_at)))
