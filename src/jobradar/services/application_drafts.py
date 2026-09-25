"""Application-draft generation, review, and local editing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import (
    ApplicationDraft,
    ApplicationDraftStatus,
    ApplicationRecord,
    ApplicationStatus,
    CandidateProfile,
    JobPosting,
    utcnow,
)
from .availability import (
    AvailabilityConcern,
    AvailabilitySelection,
    JobAvailabilityChecker,
    verify_jobs_for_application,
)

MAX_DRAFT_JOBS_PER_RUN = 5
MAX_CV_DRAFT_CHARS = 20_000
MAX_COVER_LETTER_CHARS = 10_000
MAX_AUTOMATIC_REVISIONS = 2


class DraftPipelineError(ValueError):
    """Expected user-facing draft workflow error."""



class AvailabilityConfirmationRequired(DraftPipelineError):
    """Raised before Codex work when selected jobs could not be verified."""

    def __init__(self, selection: AvailabilitySelection) -> None:
        super().__init__(
            "Die Verfügbarkeit mindestens einer Stelle konnte nicht bestätigt werden."
        )
        self.selection = selection



@dataclass(frozen=True, slots=True)
class ProfileFact:
    evidence_id: str
    category: str
    text: str


@dataclass(frozen=True, slots=True)
class DraftRequest:
    job_id: int
    title: str
    company: str
    location: str
    description: str
    language: str
    profile_version: int
    facts: tuple[ProfileFact, ...]


@dataclass(frozen=True, slots=True)
class DraftClaim:
    claim: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneratedApplicationDraft:
    cv_draft: str
    cover_letter: str
    evidence_map: tuple[DraftClaim, ...]
    questions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplicationDraftReview:
    decision: str
    scores: dict[str, int]
    must_fix: tuple[str, ...]
    optional_improvements: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    summary: str


class ApplicationDraftProvider(Protocol):
    async def generate_application_draft(
        self, request: DraftRequest
    ) -> GeneratedApplicationDraft: ...

    async def revise_application_draft(
        self,
        request: DraftRequest,
        draft: GeneratedApplicationDraft,
        review: ApplicationDraftReview,
    ) -> GeneratedApplicationDraft: ...

    async def review_application_draft(
        self,
        request: DraftRequest,
        draft: GeneratedApplicationDraft,
    ) -> ApplicationDraftReview: ...


@dataclass(frozen=True, slots=True)
class DraftRunResult:
    draft_ids: tuple[int, ...]
    skipped_inactive: tuple[AvailabilityConcern, ...] = ()


def _confirmed_profile(session: Session) -> CandidateProfile:
    profile = session.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.is_confirmed.is_(True))
        .options(
            selectinload(CandidateProfile.preferences),
            selectinload(CandidateProfile.addresses),
        )
        .order_by(CandidateProfile.updated_at.desc(), CandidateProfile.id.desc())
        .limit(1)
    )
    if profile is None:
        raise DraftPipelineError(
            "Bestätige zuerst dein Profil, bevor Bewerbungsentwürfe erstellt werden."
        )
    return profile


def _profile_facts(profile: CandidateProfile) -> tuple[ProfileFact, ...]:
    structured = dict(profile.structured_data or {})
    facts: list[ProfileFact] = []

    def add(evidence_id: str, category: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            facts.append(ProfileFact(evidence_id, category, text[:1_000]))

    add("profile.current_title", "current_title", structured.get("current_title"))
    add("profile.experience_years", "experience_years", structured.get("experience_years"))

    skills = structured.get("skills", [])
    if isinstance(skills, list):
        for index, skill in enumerate(skills[:50]):
            add(f"profile.skill.{index}", "skill", skill)

    for category, key in (("education", "education"), ("certification", "certifications")):
        values = structured.get(key, [])
        if isinstance(values, list):
            for index, value in enumerate(values[:30]):
                add(f"profile.{key}.{index}", category, value)

    preferences = profile.preferences
    if preferences is not None:
        for index, language in enumerate(preferences.languages[:20]):
            add(f"profile.language.{index}", "language", language)

    experiences = structured.get("work_experience", [])
    if isinstance(experiences, list):
        for index, raw in enumerate(experiences[:30]):
            if not isinstance(raw, dict):
                continue
            parts = [
                str(raw.get("period") or "").strip(),
                str(raw.get("role") or "").strip(),
                str(raw.get("employer") or "").strip(),
            ]
            add(
                f"profile.experience.{index}",
                "work_experience",
                " · ".join(part for part in parts if part),
            )
            responsibilities = raw.get("responsibilities", [])
            if isinstance(responsibilities, list):
                for responsibility_index, responsibility in enumerate(responsibilities[:20]):
                    add(
                        f"profile.experience.{index}.responsibility.{responsibility_index}",
                        "responsibility",
                        responsibility,
                    )

    if not facts:
        raise DraftPipelineError(
            "Das bestätigte Profil enthält noch keine verwendbaren Fähigkeiten oder Erfahrungen."
        )
    return tuple(facts)


def _draft_request(job: JobPosting, profile: CandidateProfile) -> DraftRequest:
    raw_language = str(job.language or "").strip().casefold()
    language = "en" if raw_language.startswith("en") else "de"
    return DraftRequest(
        job_id=job.id,
        title=job.title,
        company=str(job.employer or "").strip(),
        location=str(job.location_text or job.city or "").strip(),
        description=job.description_text[:20_000],
        language=language,
        profile_version=profile.version,
        facts=_profile_facts(profile),
    )


def _eligible_jobs(
    session: Session,
    *,
    job_ids: tuple[int, ...],
    min_role_score: float | None,
    min_fit_score: float | None,
    limit: int,
) -> list[JobPosting]:
    normalized_ids = tuple(dict.fromkeys(job_ids))
    if len(normalized_ids) > MAX_DRAFT_JOBS_PER_RUN:
        raise DraftPipelineError(
            f"Pro Lauf sind höchstens {MAX_DRAFT_JOBS_PER_RUN} Entwürfe erlaubt."
        )
    threshold_mode = not normalized_ids
    if threshold_mode and (min_role_score is None or min_fit_score is None):
        raise DraftPipelineError("Wähle Stellen aus oder gib beide Mindestwerte an.")

    statement = select(JobPosting).options(
        selectinload(JobPosting.scores),
        selectinload(JobPosting.application),
        selectinload(JobPosting.source),
    )
    if normalized_ids:
        statement = statement.where(JobPosting.id.in_(normalized_ids))
    else:
        statement = statement.where(JobPosting.is_active.is_(True))

    blocked_statuses = {
        ApplicationStatus.NOT_SUITABLE.value,
        ApplicationStatus.APPLIED.value,
        ApplicationStatus.INTERVIEW.value,
        ApplicationStatus.OFFER.value,
        ApplicationStatus.CLOSED.value,
    }
    candidates = []
    for job in session.scalars(statement).unique():
        if job.application_status in blocked_statuses:
            continue
        score = job.latest_score
        if threshold_mode:
            if score is None or score.candidate_fit_score is None:
                continue
            if score.relevance_score < float(min_role_score):
                continue
            if score.candidate_fit_score < float(min_fit_score):
                continue
        candidates.append(job)

    candidates.sort(
        key=lambda job: (
            job.latest_score.relevance_score if job.latest_score else -1,
            job.latest_score.candidate_fit_score
            if job.latest_score and job.latest_score.candidate_fit_score is not None
            else -1,
            job.id,
        ),
        reverse=True,
    )
    selected = candidates[: min(max(1, limit), MAX_DRAFT_JOBS_PER_RUN)]
    if not selected:
        raise DraftPipelineError("Für diese Auswahl wurden keine geeigneten Stellen gefunden.")
    return selected


def _validate_generated_draft(
    draft: GeneratedApplicationDraft, facts: tuple[ProfileFact, ...]
) -> None:
    cv_draft = draft.cv_draft.strip()
    cover_letter = draft.cover_letter.strip()
    if not cv_draft or not cover_letter:
        raise DraftPipelineError("Der Entwurf ist unvollständig.")
    if len(cv_draft) > MAX_CV_DRAFT_CHARS or len(cover_letter) > MAX_COVER_LETTER_CHARS:
        raise DraftPipelineError("Der erzeugte Entwurf überschreitet die zulässige Länge.")
    if not draft.evidence_map:
        raise DraftPipelineError("Der Entwurf enthält keine überprüfbaren Belege.")

    allowed_ids = {fact.evidence_id for fact in facts}
    for item in draft.evidence_map:
        if not item.claim.strip() or not item.evidence_ids:
            raise DraftPipelineError("Ein Entwurfsbeleg ist unvollständig.")
        if not set(item.evidence_ids) <= allowed_ids:
            raise DraftPipelineError("Der Entwurf verweist auf unbekannte Profilbelege.")


def _review_dict(
    review: ApplicationDraftReview, *, automatic_revisions: int = 0
) -> dict[str, Any]:
    return {
        "decision": review.decision,
        "scores": dict(review.scores),
        "must_fix": list(review.must_fix),
        "optional_improvements": list(review.optional_improvements),
        "unsupported_claims": list(review.unsupported_claims),
        "summary": review.summary,
        "automatic_revisions": automatic_revisions,
    }


def _draft_status(review: ApplicationDraftReview) -> str:
    scores_pass = all(
        int(review.scores.get(category, 0)) >= 4
        for category in ("relevance", "evidence", "clarity", "motivation")
    )
    if (
        review.decision == "approve"
        and scores_pass
        and not review.must_fix
        and not review.unsupported_claims
    ):
        return ApplicationDraftStatus.REVIEWED.value
    return ApplicationDraftStatus.NEEDS_REVISION.value


def _mark_application_planned(session: Session, job: JobPosting) -> None:
    record = job.application
    if record is None:
        record = ApplicationRecord(
            job_id=job.id,
            status=ApplicationStatus.PLANNED.value,
            planned_at=utcnow(),
        )
        session.add(record)
        job.application = record
        return
    if record.status in {ApplicationStatus.NEW.value, ApplicationStatus.SAVED.value}:
        record.status = ApplicationStatus.PLANNED.value
        if record.planned_at is None:
            record.planned_at = utcnow()


async def _generate_and_refine_draft(
    provider: ApplicationDraftProvider,
    request: DraftRequest,
) -> tuple[GeneratedApplicationDraft, ApplicationDraftReview, int]:
    draft = await provider.generate_application_draft(request)
    _validate_generated_draft(draft, request.facts)
    review = await provider.review_application_draft(request, draft)
    automatic_revisions = 0

    while (
        _draft_status(review) == ApplicationDraftStatus.NEEDS_REVISION.value
        and automatic_revisions < MAX_AUTOMATIC_REVISIONS
    ):
        draft = await provider.revise_application_draft(request, draft, review)
        _validate_generated_draft(draft, request.facts)
        automatic_revisions += 1
        review = await provider.review_application_draft(request, draft)

    return draft, review, automatic_revisions


def _finalize_draft_locally(
    draft: GeneratedApplicationDraft,
    profile: CandidateProfile,
) -> GeneratedApplicationDraft:
    name = str(profile.full_name or "").strip()
    contacts = [
        value
        for value in (
            str(profile.email or "").strip(),
            str(profile.phone or "").strip(),
        )
        if value
    ]
    header_lines = [value for value in (name, " · ".join(contacts)) if value]
    cv_draft = draft.cv_draft.strip()
    if header_lines:
        cv_draft = "\n".join(header_lines) + "\n\n" + cv_draft

    cover_letter = draft.cover_letter.strip()
    if name and not cover_letter.casefold().endswith(name.casefold()):
        cover_letter += f"\n{name}"

    return GeneratedApplicationDraft(
        cv_draft=cv_draft,
        cover_letter=cover_letter,
        evidence_map=draft.evidence_map,
        questions=draft.questions,
    )


def _minimize_draft_for_review(
    draft: GeneratedApplicationDraft,
    profile: CandidateProfile,
) -> GeneratedApplicationDraft:
    sensitive_values = {
        str(value).strip()
        for value in (
            profile.full_name,
            profile.email,
            profile.phone,
            profile.home_location,
            *profile.address_values,
        )
        if value and str(value).strip()
    }

    def redact(value: str) -> str:
        redacted = value
        for sensitive in sorted(sensitive_values, key=len, reverse=True):
            redacted = re.sub(re.escape(sensitive), "", redacted, flags=re.IGNORECASE)
        lines = [
            line.strip(" \t|·")
            for line in redacted.splitlines()
            if line.strip(" \t|·")
        ]
        return "\n".join(lines)

    return GeneratedApplicationDraft(
        cv_draft=redact(draft.cv_draft),
        cover_letter=redact(draft.cover_letter),
        evidence_map=draft.evidence_map,
        questions=draft.questions,
    )


async def generate_drafts(
    session: Session,
    provider: ApplicationDraftProvider,
    *,
    availability_checker: JobAvailabilityChecker,
    settings: Settings,
    job_ids: tuple[int, ...] = (),
    min_role_score: float | None = None,
    min_fit_score: float | None = None,
    limit: int = MAX_DRAFT_JOBS_PER_RUN,
    allow_unverified: bool = False,
) -> DraftRunResult:
    profile = _confirmed_profile(session)
    jobs = _eligible_jobs(
        session,
        job_ids=job_ids,
        min_role_score=min_role_score,
        min_fit_score=min_fit_score,
        limit=limit,
    )
    availability = verify_jobs_for_application(
        session,
        jobs,
        availability_checker,
        settings,
        allow_unverified=allow_unverified,
    )
    if availability.unverified and not allow_unverified:
        session.commit()
        raise AvailabilityConfirmationRequired(availability)
    jobs = list(availability.eligible_jobs)
    if not jobs:
        session.commit()
        raise DraftPipelineError(
            "Die ausgewählten Stellen sind nicht mehr verfügbar."
        )

    generated: list[
        tuple[
            JobPosting,
            DraftRequest,
            GeneratedApplicationDraft,
            ApplicationDraftReview,
            int,
        ]
    ] = []
    for job in jobs:
        request = _draft_request(job, profile)
        draft, review, automatic_revisions = await _generate_and_refine_draft(
            provider, request
        )
        generated.append((job, request, draft, review, automatic_revisions))

    records: list[ApplicationDraft] = []
    for job, request, draft, review, automatic_revisions in generated:
        revision = int(
            session.scalar(
                select(func.max(ApplicationDraft.revision)).where(
                    ApplicationDraft.job_id == job.id
                )
            )
            or 0
        ) + 1
        local_draft = _finalize_draft_locally(draft, profile)
        record = ApplicationDraft(
            job_id=job.id,
            profile_version=request.profile_version,
            revision=revision,
            status=_draft_status(review),
            language=request.language,
            cv_draft=local_draft.cv_draft,
            cover_letter=local_draft.cover_letter,
            evidence_map=[
                {"claim": item.claim, "evidence_ids": list(item.evidence_ids)}
                for item in draft.evidence_map
            ],
            questions=list(draft.questions),
            review=_review_dict(review, automatic_revisions=automatic_revisions),
        )
        session.add(record)
        _mark_application_planned(session, job)
        records.append(record)

    session.commit()
    for record in records:
        session.refresh(record)
    return DraftRunResult(
        draft_ids=tuple(record.id for record in records),
        skipped_inactive=availability.inactive,
    )


def get_draft(session: Session, draft_id: int) -> ApplicationDraft | None:
    return session.scalar(
        select(ApplicationDraft)
        .where(ApplicationDraft.id == draft_id)
        .options(selectinload(ApplicationDraft.job))
    )


def save_draft(
    session: Session,
    draft_id: int,
    *,
    cv_draft: str,
    cover_letter: str,
) -> ApplicationDraft:
    record = get_draft(session, draft_id)
    if record is None:
        raise DraftPipelineError("Der Bewerbungsentwurf wurde nicht gefunden.")
    cv_text = cv_draft.strip()
    letter_text = cover_letter.strip()
    if not cv_text or not letter_text:
        raise DraftPipelineError("Lebenslaufentwurf und Anschreiben dürfen nicht leer sein.")
    if len(cv_text) > MAX_CV_DRAFT_CHARS or len(letter_text) > MAX_COVER_LETTER_CHARS:
        raise DraftPipelineError("Der bearbeitete Entwurf überschreitet die zulässige Länge.")
    if cv_text == record.cv_draft and letter_text == record.cover_letter:
        return record
    revision = int(
        session.scalar(
            select(func.max(ApplicationDraft.revision)).where(
                ApplicationDraft.job_id == record.job_id
            )
        )
        or 0
    ) + 1
    revised = ApplicationDraft(
        job_id=record.job_id,
        profile_version=record.profile_version,
        revision=revision,
        status=ApplicationDraftStatus.EDITED.value,
        language=record.language,
        cv_draft=cv_text,
        cover_letter=letter_text,
        evidence_map=[],
        questions=list(record.questions),
        review={},
    )
    session.add(revised)
    session.commit()
    session.refresh(revised)
    return revised


async def review_draft(
    session: Session,
    provider: ApplicationDraftProvider,
    draft_id: int,
) -> ApplicationDraft:
    record = get_draft(session, draft_id)
    if record is None or record.job is None:
        raise DraftPipelineError("Der Bewerbungsentwurf wurde nicht gefunden.")
    profile = _confirmed_profile(session)
    if profile.version != record.profile_version:
        raise DraftPipelineError(
            "Das Profil wurde seit diesem Entwurf geändert. Erstelle den Entwurf neu."
        )
    request = _draft_request(record.job, profile)
    generated = GeneratedApplicationDraft(
        cv_draft=record.cv_draft,
        cover_letter=record.cover_letter,
        evidence_map=tuple(
            DraftClaim(
                claim=str(item.get("claim") or ""),
                evidence_ids=tuple(str(value) for value in item.get("evidence_ids", [])),
            )
            for item in record.evidence_map
            if isinstance(item, dict)
        ),
        questions=tuple(record.questions),
    )
    generated = _minimize_draft_for_review(generated, profile)
    review = await provider.review_application_draft(request, generated)
    record.review = _review_dict(review)
    record.status = _draft_status(review)
    session.commit()
    session.refresh(record)
    return record
