from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobradar.db import Base, build_engine
from jobradar.models import (
    ApplicationStatus,
    AppSetting,
    CandidateProfile,
    FeedbackEvent,
    JobPosting,
    JobScore,
    PreferenceProfile,
    Source,
)
from jobradar.services import rescore
from jobradar.services.rescore import RESCORE_SCORING_VERSION, process_pending_rescore


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


def _source(session: Session) -> Source:
    source = Source(name="Testquelle", slug="rescore-test", kind="test")
    session.add(source)
    session.flush()
    return source


def _job(session: Session, source: Source, external_id: str, *, active: bool) -> JobPosting:
    job = JobPosting(
        source_id=source.id,
        external_id=external_id,
        title="Assistenz der Geschaeftsfuehrung",
        description_text=(
            "Direkte Unterstuetzung der Geschaeftsfuehrung, Terminmanagement, "
            "Korrespondenz und Praesentationen"
        ),
        state="Baden-Wuerttemberg",
        remote_type="onsite",
        employment_type="Vollzeit",
        contract_type="unbefristet",
        language="de",
        structured_data={
            "required_skills": ["Organisation"],
            "industry": "Technologie",
            "role_family": "executive_assistance",
        },
        is_active=active,
    )
    session.add(job)
    session.flush()
    return job


def _pending_request(session: Session) -> AppSetting:
    request = AppSetting(
        key="scoring.rescore_request",
        value_json={"status": "pending", "requested_at": "2026-07-16T10:00:00+00:00"},
    )
    session.add(request)
    return request


def _confirmed_profile(session: Session) -> tuple[CandidateProfile, PreferenceProfile]:
    candidate = CandidateProfile(
        full_name="Ada Beispiel",
        structured_data={"skills": ["Organisation"]},
        is_confirmed=True,
        version=3,
    )
    preferences = PreferenceProfile(
        candidate=candidate,
        employment_types=["Vollzeit"],
        contract_types=["unbefristet"],
        preferred_states=["Baden-Wuerttemberg"],
        languages=["de", "en"],
        version=4,
    )
    session.add_all([candidate, preferences])
    session.flush()
    return candidate, preferences


def test_pending_request_rescores_every_active_and_inactive_job_atomically(
    session: Session,
) -> None:
    source = _source(session)
    active_job = _job(session, source, "active", active=True)
    inactive_job = _job(session, source, "inactive", active=False)
    _confirmed_profile(session)
    request = _pending_request(session)
    session.add(
        FeedbackEvent(
            job_id=active_job.id,
            event_type="application_status",
            value=ApplicationStatus.SAVED.value,
            weight_delta={"direction": 1, "magnitude": 1},
        )
    )
    session.commit()

    result = process_pending_rescore(session)

    scores = session.scalars(select(JobScore).order_by(JobScore.job_id)).all()
    assert result.status == "completed"
    assert result.processed_jobs == 2
    assert result.created_scores == 2
    assert {score.job_id for score in scores} == {active_job.id, inactive_job.id}
    assert all(score.profile_version == 3 for score in scores)
    assert all(score.preference_version == 4 for score in scores)
    assert all(score.scoring_version == RESCORE_SCORING_VERSION for score in scores)
    assert all(score.relevance_reasons for score in scores)
    assert all(score.fit_reasons[-1]["code"] == "feedback_preference" for score in scores)
    session.refresh(request)
    assert request.value_json["status"] == "completed"
    assert request.value_json["processed_jobs"] == 2


def test_no_pending_request_is_idle_and_creates_no_scores(session: Session) -> None:
    source = _source(session)
    _job(session, source, "job", active=True)
    _confirmed_profile(session)
    session.commit()

    result = process_pending_rescore(session)

    assert result.status == "idle"
    assert session.scalar(select(JobScore)) is None


def test_unconfirmed_profile_blocks_request_without_scoring(session: Session) -> None:
    source = _source(session)
    _job(session, source, "job", active=True)
    candidate = CandidateProfile(is_confirmed=False, structured_data={})
    session.add(candidate)
    request = _pending_request(session)
    session.commit()

    result = process_pending_rescore(session)

    assert result.status == "blocked"
    assert "bestaetigtes" in (result.reason or "")
    assert session.scalar(select(JobScore)) is None
    session.refresh(request)
    assert request.value_json["status"] == "blocked"


def test_completed_request_is_not_processed_twice(session: Session) -> None:
    source = _source(session)
    _job(session, source, "job", active=True)
    _confirmed_profile(session)
    _pending_request(session)
    session.commit()

    first = process_pending_rescore(session)
    second = process_pending_rescore(session)

    assert first.status == "completed"
    assert second.status == "idle"
    assert len(session.scalars(select(JobScore)).all()) == 1


def test_scoring_error_rolls_back_partial_scores_and_running_state(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(session)
    _job(session, source, "first", active=True)
    _job(session, source, "second", active=True)
    _confirmed_profile(session)
    request = _pending_request(session)
    session.commit()

    original_evaluate = rescore.evaluate_job
    calls = 0

    def fail_on_second(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulierter Bewertungsfehler")
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(rescore, "evaluate_job", fail_on_second)

    with pytest.raises(RuntimeError, match="simulierter"):
        process_pending_rescore(session)

    session.refresh(request)
    assert request.value_json["status"] == "pending"
    assert session.scalars(select(JobScore)).all() == []
