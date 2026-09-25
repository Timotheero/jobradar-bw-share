"""Automatic, cached summaries for the job detail view."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..integrations.codex.provider import (
    CodexJobSummary,
    JobForAnalysis,
    ProfileForAnalysis,
)
from ..models import CandidateProfile, JobPosting, JobScore, JobSummary, PreferenceProfile
from . import profile as profile_service

SUMMARY_MODEL = "gpt-5.6-luna"
SUMMARY_REASONING_EFFORT = "medium"


class JobSummaryProvider(Protocol):
    async def summarize_job(
        self,
        job: JobForAnalysis,
        profile: ProfileForAnalysis,
        *,
        locale: str,
        model: str,
        reasoning_effort: str,
    ) -> CodexJobSummary: ...

    async def summarize_jobs(
        self,
        jobs: list[JobForAnalysis],
        profile: ProfileForAnalysis,
        *,
        locale: str,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, CodexJobSummary]: ...


@dataclass(frozen=True, slots=True)
class SummaryPrecomputeResult:
    eligible_jobs: int
    generated_jobs: int


class JobSummaryNotFoundError(LookupError):
    pass


def current_summary(session: Session, job: JobPosting, *, locale: str) -> JobSummary | None:
    """Return the cached summary only when job and profile context still match."""

    candidate = profile_service.get_candidate_profile(session)
    preference = profile_service.get_preference_profile(session)
    profile_version = candidate.version if candidate and candidate.is_confirmed else None
    preference_version = preference.version if preference else None
    effective_locale = locale if locale in {"de", "en"} else "de"
    summary = job.ai_summary
    if summary is None or not _is_current(
        summary,
        content_hash=_content_version(job),
        profile_version=profile_version,
        preference_version=preference_version,
        locale=effective_locale,
    ):
        return None
    return summary


async def get_or_create_summary(
    session: Session,
    job_id: int,
    provider: JobSummaryProvider,
    *,
    locale: str,
) -> JobSummary:
    """Return a current summary, generating it once per job/profile context."""

    job = session.scalar(
        select(JobPosting)
        .where(JobPosting.id == job_id)
        .options(selectinload(JobPosting.ai_summary))
    )
    if job is None:
        raise JobSummaryNotFoundError(job_id)

    candidate = profile_service.get_candidate_profile(session)
    preference = profile_service.get_preference_profile(session)
    effective_locale = locale if locale in {"de", "en"} else "de"
    content_hash = _content_version(job)
    profile_version = candidate.version if candidate and candidate.is_confirmed else None
    preference_version = preference.version if preference else None
    cached = job.ai_summary
    if cached is not None and _is_current(
        cached,
        content_hash=content_hash,
        profile_version=profile_version,
        preference_version=preference_version,
        locale=effective_locale,
    ):
        return cached

    result = await provider.summarize_job(
        _job_projection(job),
        _profile_projection(candidate, preference),
        locale=effective_locale,
        model=SUMMARY_MODEL,
        reasoning_effort=SUMMARY_REASONING_EFFORT,
    )
    values = {
        "content_hash": content_hash,
        "profile_version": profile_version,
        "preference_version": preference_version,
        "locale": effective_locale,
        "model": SUMMARY_MODEL,
        "reasoning_effort": SUMMARY_REASONING_EFFORT,
        "overview": result.overview,
        "key_points": list(result.key_points),
        "missing_information": list(result.missing_information),
    }
    if cached is None:
        cached = JobSummary(job=job, **values)
        session.add(cached)
    else:
        _apply_values(cached, values)

    try:
        session.commit()
    except IntegrityError:
        # Two tabs may request the first summary concurrently. Keep one row per job.
        session.rollback()
        cached = session.scalar(select(JobSummary).where(JobSummary.job_id == job.id))
        if cached is None:
            raise
        _apply_values(cached, values)
        session.commit()
    session.refresh(cached)
    return cached


async def precompute_summaries(
    session: Session,
    provider: JobSummaryProvider,
    *,
    min_relevance: float = 60,
    limit: int = 20,
    locale: str = "de",
) -> SummaryPrecomputeResult:
    """Generate current summaries for the strongest locally scored jobs in bounded turns."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    candidate = profile_service.get_candidate_profile(session)
    preference = profile_service.get_preference_profile(session)
    effective_locale = locale if locale in {"de", "en"} else "de"
    profile_version = candidate.version if candidate and candidate.is_confirmed else None
    preference_version = preference.version if preference else None

    latest_scores = (
        select(JobScore.job_id, func.max(JobScore.id).label("score_id"))
        .group_by(JobScore.job_id)
        .subquery()
    )
    jobs = list(
        session.scalars(
            select(JobPosting)
            .join(latest_scores, latest_scores.c.job_id == JobPosting.id)
            .join(JobScore, JobScore.id == latest_scores.c.score_id)
            .where(
                JobPosting.is_active.is_(True),
                JobScore.relevance_score >= min_relevance,
            )
            .options(selectinload(JobPosting.ai_summary))
            .order_by(JobScore.relevance_score.desc(), JobPosting.id)
        ).all()
    )
    pending = [
        job
        for job in jobs
        if job.ai_summary is None
        or not _is_current(
            job.ai_summary,
            content_hash=_content_version(job),
            profile_version=profile_version,
            preference_version=preference_version,
            locale=effective_locale,
        )
    ][:limit]
    if not pending:
        return SummaryPrecomputeResult(eligible_jobs=len(jobs), generated_jobs=0)

    profile_projection = _profile_projection(candidate, preference)
    results: dict[str, CodexJobSummary] = {}
    for start in range(0, len(pending), 20):
        batch = pending[start : start + 20]
        results.update(
            await provider.summarize_jobs(
                [_job_projection(job) for job in batch],
                profile_projection,
                locale=effective_locale,
                model=SUMMARY_MODEL,
                reasoning_effort=SUMMARY_REASONING_EFFORT,
            )
        )
    values_by_job_id = {
        job.id: _summary_values(
            job,
            results[str(job.id)],
            profile_version=profile_version,
            preference_version=preference_version,
            locale=effective_locale,
        )
        for job in pending
    }
    _persist_summaries(session, values_by_job_id)
    return SummaryPrecomputeResult(
        eligible_jobs=len(jobs),
        generated_jobs=len(values_by_job_id),
    )


def _summary_values(
    job: JobPosting,
    result: CodexJobSummary,
    *,
    profile_version: int | None,
    preference_version: int | None,
    locale: str,
) -> dict[str, object]:
    return {
        "content_hash": _content_version(job),
        "profile_version": profile_version,
        "preference_version": preference_version,
        "locale": locale,
        "model": SUMMARY_MODEL,
        "reasoning_effort": SUMMARY_REASONING_EFFORT,
        "overview": result.overview,
        "key_points": list(result.key_points),
        "missing_information": list(result.missing_information),
    }


def _persist_summaries(
    session: Session, values_by_job_id: dict[int, dict[str, object]]
) -> None:
    def apply() -> None:
        for job_id, values in values_by_job_id.items():
            summary = session.scalar(select(JobSummary).where(JobSummary.job_id == job_id))
            if summary is None:
                session.add(JobSummary(job_id=job_id, **values))
            else:
                _apply_values(summary, values)

    apply()
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        apply()
        session.commit()


def _content_version(job: JobPosting) -> str:
    if job.content_hash:
        return job.content_hash
    payload = "\n".join(
        (
            job.title,
            job.employer or "",
            job.location_text or "",
            job.description_text,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_current(
    summary: JobSummary,
    *,
    content_hash: str,
    profile_version: int | None,
    preference_version: int | None,
    locale: str,
) -> bool:
    return (
        summary.content_hash == content_hash
        and summary.profile_version == profile_version
        and summary.preference_version == preference_version
        and summary.locale == locale
        and summary.model == SUMMARY_MODEL
        and summary.reasoning_effort == SUMMARY_REASONING_EFFORT
    )


def _apply_values(summary: JobSummary, values: dict[str, object]) -> None:
    for key, value in values.items():
        setattr(summary, key, value)


def _job_projection(job: JobPosting) -> JobForAnalysis:
    structured = dict(job.structured_data or {})
    employment = " · ".join(
        value for value in (job.employment_type, job.contract_type) if value and value.strip()
    )
    extra_facts = _compact_values(
        (
            _labelled("Vergütung", structured.get("salary")),
            _labelled("Reisetätigkeit", structured.get("travel")),
            _labelled("Fahrtzeit", structured.get("commute_label")),
        )
    )
    description = job.description_text
    if extra_facts:
        description = f"{description}\n\nStrukturierte Angaben: {'; '.join(extra_facts)}"
    return JobForAnalysis(
        job_id=str(job.id),
        title=job.title,
        company=job.employer or "Nicht angegeben",
        location=job.location_text or job.city or "Nicht angegeben",
        work_mode=job.remote_type or "Nicht angegeben",
        employment_scope=employment or "Nicht angegeben",
        description=description,
    )


def _profile_projection(
    candidate: CandidateProfile | None, preference: PreferenceProfile | None
) -> ProfileForAnalysis:
    summary = "Kein bestätigtes Kandidatenprofil."
    skills: tuple[str, ...] = ()
    experience: tuple[str, ...] = ()
    if candidate is not None and candidate.is_confirmed:
        structured = dict(candidate.structured_data or {})
        summary_parts = _compact_values(
            (
                _labelled("Aktuelle Rolle", structured.get("current_title")),
                _labelled("Berufserfahrung in Jahren", structured.get("experience_years")),
            )
        )
        summary = "; ".join(summary_parts) or "Bestätigtes Profil ohne weitere Angaben."
        skills = tuple(_string_values(structured.get("skills"), maximum=30))
        experience = tuple(_experience_roles(structured.get("work_experience")))

    preferences: list[str] = []
    languages: tuple[str, ...] = ()
    if preference is not None:
        languages = tuple(_string_values(preference.languages, maximum=20))
        preferences.extend(
            (
                f"Arbeitszeit: {', '.join(preference.employment_types)}",
                f"Vertrag: {', '.join(preference.contract_types)}",
                f"Region: {', '.join(preference.preferred_states)}",
                f"Maximale Pendelzeit: {preference.max_commute_minutes} Minuten",
            )
        )
        preferred_mode = max(
            (
                ("Präsenz", preference.onsite_weight),
                ("Hybrid", preference.hybrid_weight),
                ("Remote", preference.remote_weight),
            ),
            key=lambda item: item[1],
        )[0]
        preferences.append(f"Bevorzugtes Arbeitsmodell: {preferred_mode}")
        if preference.min_salary is not None:
            preferences.append(f"Mindestgehalt: {preference.min_salary} EUR")
        if preference.target_salary is not None:
            preferences.append(f"Zielgehalt: {preference.target_salary} EUR")

    return ProfileForAnalysis(
        summary=summary,
        skills=skills,
        experience=experience,
        languages=languages,
        preferences=tuple(preferences),
    )


def _experience_roles(value: object) -> Sequence[str]:
    if not isinstance(value, list):
        return ()
    roles: list[str] = []
    for item in value[:20]:
        if isinstance(item, dict) and item.get("role"):
            roles.append(str(item["role"]).strip()[:300])
        elif isinstance(item, str) and item.strip():
            roles.append(item.strip()[:300])
    return roles


def _string_values(value: object, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:300] for item in value[:maximum] if str(item).strip()]


def _labelled(label: str, value: object) -> str:
    text = str(value).strip() if value is not None else ""
    return f"{label}: {text[:500]}" if text else ""


def _compact_values(values: Sequence[str]) -> list[str]:
    return [value for value in values if value]
