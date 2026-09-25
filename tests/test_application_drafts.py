from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from jobradar.config import Settings
from jobradar.db import Base, build_engine
from jobradar.models import (
    ApplicationDraft,
    ApplicationDraftStatus,
    ApplicationRecord,
    ApplicationStatus,
    CandidateProfile,
    JobAvailabilityStatus,
    JobPosting,
    JobScore,
    Source,
)
from jobradar.services.application_drafts import (
    ApplicationDraftReview,
    AvailabilityConfirmationRequired,
    DraftClaim,
    DraftPipelineError,
    DraftRequest,
    GeneratedApplicationDraft,
    ProfileFact,
    _generate_and_refine_draft,
    generate_drafts,
    review_draft,
    save_draft,
)
from jobradar.services.availability import AvailabilityCheck
from jobradar.services.settings import CRAWLING_SETTING_KEY, set_setting


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


class DraftProvider:
    def __init__(self, *, evidence_id: str | None = None) -> None:
        self.evidence_id = evidence_id
        self.generated_requests: list[DraftRequest] = []
        self.review_requests: list[tuple[DraftRequest, GeneratedApplicationDraft]] = []
        self.revision_requests: list[
            tuple[DraftRequest, GeneratedApplicationDraft, ApplicationDraftReview]
        ] = []

    async def generate_application_draft(
        self, request: DraftRequest
    ) -> GeneratedApplicationDraft:
        self.generated_requests.append(request)
        evidence_id = self.evidence_id or request.facts[0].evidence_id
        return GeneratedApplicationDraft(
            cv_draft=" Assistenz der Geschäftsführung mit Organisationserfahrung. ",
            cover_letter=" Ich unterstütze Ihre Geschäftsführung strukturiert. ",
            evidence_map=(
                DraftClaim(
                    claim="Organisationserfahrung",
                    evidence_ids=(evidence_id,),
                ),
            ),
            questions=("Welche Reisetätigkeit ist vorgesehen?",),
        )

    async def revise_application_draft(
        self,
        request: DraftRequest,
        draft: GeneratedApplicationDraft,
        review: ApplicationDraftReview,
    ) -> GeneratedApplicationDraft:
        self.revision_requests.append((request, draft, review))
        return GeneratedApplicationDraft(
            cv_draft="Überarbeiteter Lebenslauf mit relevanter Kalendersteuerung.",
            cover_letter="Überarbeitetes Anschreiben mit einem konkreten, belegten Beispiel.",
            evidence_map=draft.evidence_map,
            questions=draft.questions,
        )


    async def review_application_draft(
        self,
        request: DraftRequest,
        draft: GeneratedApplicationDraft,
    ) -> ApplicationDraftReview:
        self.review_requests.append((request, draft))
        return ApplicationDraftReview(
            decision="approve",
            scores={"relevance": 5, "evidence": 5, "clarity": 4, "motivation": 4},
            must_fix=(),
            optional_improvements=("Ein Beispiel ergänzen.",),
            unsupported_claims=(),
            summary="Belegt und relevant.",
        )


class RevisingDraftProvider(DraftProvider):
    def __init__(self, *, always_revise: bool = False) -> None:
        super().__init__()
        self.always_revise = always_revise

    async def review_application_draft(
        self,
        request: DraftRequest,
        draft: GeneratedApplicationDraft,
    ) -> ApplicationDraftReview:
        self.review_requests.append((request, draft))
        should_revise = self.always_revise or len(self.review_requests) == 1
        return ApplicationDraftReview(
            decision="revise" if should_revise else "approve",
            scores={
                "relevance": 3 if should_revise else 5,
                "evidence": 5,
                "clarity": 3 if should_revise else 4,
                "motivation": 3 if should_revise else 4,
            },
            must_fix=("Konkreter auf die Stelle zuschneiden.",) if should_revise else (),
            optional_improvements=(),
            unsupported_claims=(),
            summary="Überarbeitung erforderlich." if should_revise else "Freigegeben.",
        )


def _draft_request_for_revision() -> DraftRequest:
    return DraftRequest(
        job_id=1,
        title="Executive Assistant",
        company="Beispiel GmbH",
        location="Stuttgart",
        description="Kalendersteuerung und direkte Unterstützung der Geschäftsführung.",
        language="de",
        profile_version=1,
        facts=(ProfileFact("profile.skill.0", "skill", "Kalendersteuerung"),),
    )


def _seed_profile_and_jobs(session: Session) -> tuple[JobPosting, JobPosting]:
    profile = CandidateProfile(
        full_name="Private Person",
        email="private@example.invalid",
        phone="+49 123 456",
        home_location="Private Straße 1",
        cv_text="RAW PRIVATE CV CONTENT",
        structured_data={
            "current_title": "Executive Assistant",
            "experience_years": 8,
            "skills": ["Kalendersteuerung", "Organisation"],
            "work_experience": [
                {"period": "2020–2026", "role": "Executive Assistant", "employer": "Muster"}
            ],
        },
        is_confirmed=True,
        version=3,
    )
    source = Source(name="Test", slug="draft-test", kind="api", enabled=True)
    session.add_all([profile, source])
    session.flush()
    high = JobPosting(
        source_id=source.id,
        external_id="high",
        title="Assistenz der Geschäftsführung",
        employer="Beispiel GmbH",
        description_text="Direkte Unterstützung der Geschäftsführung und Terminsteuerung.",
        location_text="Stuttgart",
        language="de",
        is_active=True,
    )
    low = JobPosting(
        source_id=source.id,
        external_id="low",
        title="Teamassistenz",
        employer="Andere GmbH",
        description_text="Allgemeine Teamorganisation.",
        location_text="Karlsruhe",
        language="de",
        is_active=True,
    )
    session.add_all([high, low])
    session.flush()
    session.add_all(
        [
            JobScore(
                job_id=high.id,
                candidate_profile_id=profile.id,
                profile_version=profile.version,
                relevance_score=86,
                candidate_fit_score=91,
            ),
            JobScore(
                job_id=low.id,
                candidate_profile_id=profile.id,
                profile_version=profile.version,
                relevance_score=55,
                candidate_fit_score=60,
            ),
        ]
    )
    session.commit()
    return high, low


@pytest.mark.asyncio
async def test_threshold_generation_uses_minimized_profile_and_marks_job_planned(
    session: Session,
) -> None:
    high, _ = _seed_profile_and_jobs(session)
    provider = DraftProvider()

    result = await generate_drafts(
        session,
        provider,
        availability_checker=object(),  # type: ignore[arg-type] - gate keeps it unused
        settings=Settings(crawling_enabled=False),
        allow_unverified=True,
        min_role_score=70,
        min_fit_score=75,
    )

    assert len(result.draft_ids) == 1
    assert [request.job_id for request in provider.generated_requests] == [high.id]
    request_text = repr(provider.generated_requests[0])
    assert "Private Person" not in request_text
    assert "private@example.invalid" not in request_text
    assert "+49 123 456" not in request_text
    assert "Private Straße 1" not in request_text
    assert "RAW PRIVATE CV CONTENT" not in request_text

    draft = session.get(ApplicationDraft, result.draft_ids[0])
    assert draft is not None
    assert draft.profile_version == 3
    assert draft.revision == 1
    assert draft.status == ApplicationDraftStatus.REVIEWED.value
    assert draft.cv_draft.startswith("Private Person\nprivate@example.invalid · +49 123 456")
    assert draft.cover_letter.endswith("Private Person")
    review_text = repr(provider.review_requests)
    assert "Private Person" not in review_text
    assert "private@example.invalid" not in review_text
    assert "+49 123 456" not in review_text
    assert "Private Straße 1" not in review_text
    assert draft.review["decision"] == "approve"
    application = session.scalar(
        select(ApplicationRecord).where(ApplicationRecord.job_id == high.id)
    )
    assert application is not None
    assert application.status == ApplicationStatus.PLANNED.value


@pytest.mark.asyncio
async def test_unknown_profile_evidence_rejects_entire_generation(session: Session) -> None:
    high, _ = _seed_profile_and_jobs(session)
    provider = DraftProvider(evidence_id="profile.contact.email")

    with pytest.raises(DraftPipelineError, match="unbekannte Profilbelege"):
        await generate_drafts(
            session,
            provider,
            availability_checker=object(),  # type: ignore[arg-type] - gate keeps it unused
            settings=Settings(crawling_enabled=False),
            job_ids=(high.id,),
            allow_unverified=True,
        )

    assert session.scalar(select(func.count(ApplicationDraft.id))) == 0
    assert session.scalar(select(func.count(ApplicationRecord.id))) == 0
    assert provider.review_requests == []


@pytest.mark.asyncio
async def test_rereview_redacts_locally_added_contact_details(session: Session) -> None:
    high, _ = _seed_profile_and_jobs(session)
    result = await generate_drafts(
        session,
        DraftProvider(),
        availability_checker=object(),  # type: ignore[arg-type] - gate keeps it unused
        settings=Settings(crawling_enabled=False),
        job_ids=(high.id,),
        allow_unverified=True,
    )
    provider = DraftProvider()

    await review_draft(session, provider, result.draft_ids[0])

    sent_draft = provider.review_requests[-1][1]
    sent_text = f"{sent_draft.cv_draft}\n{sent_draft.cover_letter}"
    assert "Private Person" not in sent_text
    assert "private@example.invalid" not in sent_text
    assert "+49 123 456" not in sent_text
    assert "Private Straße 1" not in sent_text


@pytest.mark.asyncio
async def test_edit_creates_revision_and_preserves_reviewed_version(session: Session) -> None:
    high, _ = _seed_profile_and_jobs(session)
    result = await generate_drafts(
        session,
        DraftProvider(),
        availability_checker=object(),  # type: ignore[arg-type] - gate keeps it unused
        settings=Settings(crawling_enabled=False),
        job_ids=(high.id,),
        allow_unverified=True,
    )
    reviewed = session.get(ApplicationDraft, result.draft_ids[0])
    assert reviewed is not None

    edited = save_draft(
        session,
        reviewed.id,
        cv_draft="Manuell bearbeiteter Lebenslauf",
        cover_letter="Manuell bearbeitetes Anschreiben",
    )

    assert edited.id != reviewed.id
    assert edited.revision == 2
    assert edited.status == ApplicationDraftStatus.EDITED.value
    assert edited.review == {}
    assert edited.evidence_map == []
    assert reviewed.revision == 1
    assert reviewed.status == ApplicationDraftStatus.REVIEWED.value
    assert reviewed.cv_draft.startswith("Private Person")

    unchanged = save_draft(
        session,
        edited.id,
        cv_draft=edited.cv_draft,
        cover_letter=edited.cover_letter,
    )
    assert unchanged.id == edited.id
    assert session.scalar(select(func.count(ApplicationDraft.id))) == 2


@pytest.mark.asyncio
async def test_generation_persists_automatically_refined_documents(
    session: Session,
) -> None:
    high, _ = _seed_profile_and_jobs(session)
    provider = RevisingDraftProvider()

    result = await generate_drafts(
        session,
        provider,
        availability_checker=object(),  # type: ignore[arg-type] - gate keeps it unused
        settings=Settings(crawling_enabled=False),
        job_ids=(high.id,),
        allow_unverified=True,
    )

    draft = session.get(ApplicationDraft, result.draft_ids[0])
    assert draft is not None
    assert draft.status == ApplicationDraftStatus.REVIEWED.value
    assert draft.cv_draft.startswith("Private Person")
    assert draft.review["automatic_revisions"] == 1
    assert len(provider.review_requests) == 2


@pytest.mark.asyncio
async def test_failed_review_is_automatically_revised_and_reviewed_again() -> None:
    provider = RevisingDraftProvider()

    draft, review, automatic_revisions = await _generate_and_refine_draft(
        provider, _draft_request_for_revision()
    )

    assert automatic_revisions == 1
    assert len(provider.revision_requests) == 1
    assert len(provider.review_requests) == 2
    assert draft.cv_draft.startswith("Überarbeiteter")
    assert review.decision == "approve"


@pytest.mark.asyncio
async def test_automatic_revision_stops_after_two_attempts() -> None:
    provider = RevisingDraftProvider(always_revise=True)

    _draft, review, automatic_revisions = await _generate_and_refine_draft(
        provider, _draft_request_for_revision()
    )

    assert automatic_revisions == 2
    assert len(provider.revision_requests) == 2
    assert len(provider.review_requests) == 3
    assert review.decision == "revise"


class UnverifiedChecker:
    def check(self, _job: JobPosting) -> AvailabilityCheck:
        return AvailabilityCheck(
            JobAvailabilityStatus.CHECK_FAILED,
            "test",
            "temporary_failure",
        )

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_unverified_job_requires_explicit_confirmation_before_codex(
    session: Session,
) -> None:
    high, _ = _seed_profile_and_jobs(session)
    set_setting(session, CRAWLING_SETTING_KEY, True)
    provider = DraftProvider()
    settings = Settings(crawling_enabled=True)
    checker = UnverifiedChecker()

    with pytest.raises(AvailabilityConfirmationRequired):
        await generate_drafts(
            session,
            provider,
            availability_checker=checker,
            settings=settings,
            job_ids=(high.id,),
        )

    assert provider.generated_requests == []

    result = await generate_drafts(
        session,
        provider,
        availability_checker=checker,
        settings=settings,
        job_ids=(high.id,),
        allow_unverified=True,
    )

    assert len(result.draft_ids) == 1
    assert [request.job_id for request in provider.generated_requests] == [high.id]
