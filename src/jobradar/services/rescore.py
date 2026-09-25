"""Atomic, local processing of pending whole-database rescore requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domain.scoring import SCORING_VERSION, evaluate_job
from ..models import AppSetting, CandidateProfile, JobPosting, JobScore, PreferenceProfile
from .learning import (
    LEARNING_VERSION,
    FeedbackExample,
    calculate_preference_signal,
    collect_feedback_examples,
)

RESCORE_SETTING_KEY = "scoring.rescore_request"
RESCORE_SCORING_VERSION = f"{SCORING_VERSION}+{LEARNING_VERSION}"


@dataclass(frozen=True, slots=True)
class RescoreResult:
    """Summary of one pending-request check or completed rescore."""

    status: Literal["idle", "blocked", "completed"]
    processed_jobs: int = 0
    created_scores: int = 0
    profile_version: int | None = None
    preference_version: int | None = None
    feedback_examples: int = 0
    reason: str | None = None


def request_rescore(
    session: Session,
    *,
    reason: str,
    profile_version: int | None = None,
    preference_version: int | None = None,
    commit: bool = False,
) -> AppSetting:
    """Create or replace the single pending local-rescore request.

    Callers can keep this update in the same transaction as a profile or
    feedback change.  Processing remains a worker responsibility and never
    performs an external request.
    """

    payload = {
        "requested_at": _now_iso(),
        "reason": reason,
        "profile_version": profile_version,
        "preference_version": preference_version,
        "status": "pending",
    }
    request = session.get(AppSetting, RESCORE_SETTING_KEY)
    if request is None:
        request = AppSetting(
            key=RESCORE_SETTING_KEY,
            value_json=payload,
            description="Local rescore request after a profile or feedback change.",
        )
        session.add(request)
    else:
        request.value_json = payload
    if commit:
        session.commit()
        session.refresh(request)
    else:
        session.flush()
    return request


def score_job_with_learning(
    session: Session,
    job: JobPosting,
    *,
    candidate: CandidateProfile,
    preferences: PreferenceProfile,
    feedback_examples: tuple[FeedbackExample, ...] | None = None,
    commit: bool = True,
) -> JobScore:
    """Persist one local score with a separately visible feedback adjustment."""

    if not candidate.is_confirmed:
        raise ValueError("Learning scores require a confirmed candidate profile")
    examples = (
        collect_feedback_examples(session) if feedback_examples is None else feedback_examples
    )
    relevance, candidate_fit = evaluate_job(job, candidate, preferences)
    signal = calculate_preference_signal(job, examples)
    adjusted_fit = round(max(0.0, min(100.0, candidate_fit.value + signal.adjustment)), 1)
    applied_adjustment = round(adjusted_fit - candidate_fit.value, 1)
    score = JobScore(
        job_id=job.id,
        candidate_profile_id=candidate.id,
        profile_version=candidate.version,
        preference_version=preferences.version,
        relevance_score=relevance.value,
        candidate_fit_score=adjusted_fit,
        relevance_reasons=relevance.reasons_as_dicts(),
        fit_reasons=[
            *candidate_fit.reasons_as_dicts(),
            signal.as_reason_dict(applied_adjustment=applied_adjustment),
        ],
        scoring_version=RESCORE_SCORING_VERSION,
    )
    session.add(score)
    if commit:
        session.commit()
        session.refresh(score)
    else:
        session.flush()
    return score


def process_pending_rescore(session: Session) -> RescoreResult:
    """Consume one pending request and score every stored job without network access.

    The caller should provide a dedicated worker session.  Scores and the final
    request state are committed atomically, so a scoring exception cannot leave
    a partially completed result set.
    """

    request = session.scalar(
        select(AppSetting)
        .where(AppSetting.key == RESCORE_SETTING_KEY)
        .with_for_update(skip_locked=True)
    )
    if request is None:
        return RescoreResult(status="idle", reason="Keine Neuberechnung angefordert.")

    payload = _request_payload(request.value_json)
    if payload.get("status") != "pending":
        return RescoreResult(status="idle", reason="Keine ausstehende Neuberechnung.")

    candidate = session.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.is_confirmed.is_(True))
        .options(selectinload(CandidateProfile.preferences))
        .order_by(CandidateProfile.id)
        .limit(1)
    )
    if candidate is None:
        return _block_request(
            session,
            request,
            payload,
            reason="Ein bestaetigtes Kandidatenprofil fehlt.",
        )
    preferences = candidate.preferences
    if preferences is None:
        return _block_request(
            session,
            request,
            payload,
            reason="Ein Praeferenzprofil fehlt.",
            profile_version=candidate.version,
        )

    try:
        started_at = _now_iso()
        request.value_json = {
            **payload,
            "status": "running",
            "started_at": started_at,
            "profile_version": candidate.version,
            "preference_version": preferences.version,
        }
        session.flush()

        examples = collect_feedback_examples(session)
        jobs = session.scalars(select(JobPosting).order_by(JobPosting.id)).all()
        for job in jobs:
            score_job_with_learning(
                session,
                job,
                candidate=candidate,
                preferences=preferences,
                feedback_examples=examples,
                commit=False,
            )

        finished_at = _now_iso()
        request.value_json = {
            **payload,
            "status": "completed",
            "started_at": started_at,
            "completed_at": finished_at,
            "processed_jobs": len(jobs),
            "created_scores": len(jobs),
            "profile_version": candidate.version,
            "preference_version": preferences.version,
            "feedback_examples": len(examples),
            "scoring_version": RESCORE_SCORING_VERSION,
        }
        session.commit()
    except Exception:
        session.rollback()
        raise
    return RescoreResult(
        status="completed",
        processed_jobs=len(jobs),
        created_scores=len(jobs),
        profile_version=candidate.version,
        preference_version=preferences.version,
        feedback_examples=len(examples),
    )


def _block_request(
    session: Session,
    request: AppSetting,
    payload: dict[str, Any],
    *,
    reason: str,
    profile_version: int | None = None,
) -> RescoreResult:
    request.value_json = {
        **payload,
        "status": "blocked",
        "last_attempt_at": _now_iso(),
        "blocked_reason": reason,
        "profile_version": profile_version,
    }
    session.commit()
    return RescoreResult(
        status="blocked",
        profile_version=profile_version,
        reason=reason,
    )


def _request_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {"status": "invalid"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
