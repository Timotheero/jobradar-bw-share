from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from jobradar.db import Base, build_engine
from jobradar.models import ApplicationStatus, FeedbackEvent, JobPosting, Source
from jobradar.services.learning import (
    MAX_LEARNING_ADJUSTMENT,
    calculate_preference_signal,
    collect_feedback_examples,
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


def _source(session: Session) -> Source:
    source = Source(name="Testquelle", slug="learning-test", kind="test")
    session.add(source)
    session.flush()
    return source


def _job(
    session: Session,
    source: Source,
    external_id: str,
    *,
    remote_type: str = "onsite",
    employment_type: str = "Vollzeit",
    contract_type: str = "unbefristet",
    state: str = "Baden-Wuerttemberg",
    industry: str = "Technologie",
) -> JobPosting:
    job = JobPosting(
        source_id=source.id,
        external_id=external_id,
        title="Assistenz der Geschaeftsfuehrung",
        description_text="Termine, Korrespondenz und Geschaeftsfuehrungsunterstuetzung",
        remote_type=remote_type,
        employment_type=employment_type,
        contract_type=contract_type,
        state=state,
        language="de",
        structured_data={"industry": industry, "role_family": "executive_assistance"},
    )
    session.add(job)
    session.flush()
    return job


def _feedback(session: Session, job: JobPosting, decision: str, *, magnitude: int = 1) -> None:
    session.add(
        FeedbackEvent(
            job_id=job.id,
            event_type="application_status",
            value=decision,
            weight_delta={"direction": 1, "magnitude": magnitude},
        )
    )
    session.flush()


def test_signal_uses_visible_matches_and_explains_each_contribution(session: Session) -> None:
    source = _source(session)
    target = _job(session, source, "target")
    liked = _job(session, source, "liked")
    rejected_unlike = _job(
        session,
        source,
        "rejected",
        remote_type="remote",
        employment_type="Teilzeit",
        contract_type="befristet",
        state="Bayern",
        industry="Gesundheit",
    )
    _feedback(session, liked, ApplicationStatus.SAVED.value)
    _feedback(session, rejected_unlike, ApplicationStatus.NOT_SUITABLE.value)
    session.commit()

    examples = collect_feedback_examples(session)
    signal = calculate_preference_signal(target, examples)
    reason = signal.as_reason_dict()

    assert len(examples) == 2
    assert 0 < signal.adjustment <= MAX_LEARNING_ADJUSTMENT
    assert signal.matched_feedback == 1
    assert signal.positive_matches == 1
    assert reason["code"] == "feedback_preference"
    assert reason["effect"] == "positive"
    assert "Arbeitsmodell=onsite" in reason["evidence"][0]
    assert "+/-10" in reason["explanation"]


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (ApplicationStatus.SAVED.value, 10.0),
        (ApplicationStatus.NOT_SUITABLE.value, -10.0),
    ],
)
def test_signal_is_hard_capped_in_both_directions(
    session: Session, decision: str, expected: float
) -> None:
    source = _source(session)
    target = _job(session, source, "target")
    for index in range(20):
        example = _job(session, source, f"feedback-{index}")
        _feedback(session, example, decision, magnitude=1_000_000)
    session.commit()

    signal = calculate_preference_signal(target, collect_feedback_examples(session))

    assert signal.adjustment == expected
    assert abs(signal.adjustment) <= MAX_LEARNING_ADJUSTMENT


def test_only_latest_supported_decision_per_job_is_used(session: Session) -> None:
    source = _source(session)
    target = _job(session, source, "target")
    example = _job(session, source, "example")
    _feedback(session, example, ApplicationStatus.SAVED.value)
    _feedback(session, example, ApplicationStatus.NOT_SUITABLE.value)
    session.add(
        FeedbackEvent(
            job_id=example.id,
            event_type="free_form",
            value="boost",
            weight_delta={"magnitude": 999_999},
        )
    )
    session.commit()

    examples = collect_feedback_examples(session)
    signal = calculate_preference_signal(target, examples)

    assert len(examples) == 1
    assert examples[0].decision == ApplicationStatus.NOT_SUITABLE.value
    assert signal.adjustment < 0


def test_target_jobs_own_feedback_is_not_echoed(session: Session) -> None:
    source = _source(session)
    target = _job(session, source, "target")
    _feedback(session, target, ApplicationStatus.SAVED.value)
    session.commit()

    signal = calculate_preference_signal(target, collect_feedback_examples(session))

    assert signal.adjustment == 0
    assert signal.considered_feedback == 0
    assert signal.matched_feedback == 0
