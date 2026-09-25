from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from jobradar.api import router
from jobradar.config import Settings
from jobradar.db import Base, build_engine, get_db
from jobradar.domain.scoring import evaluate_job, score_candidate_fit, score_role_relevance
from jobradar.models import (
    ApplicationStatus,
    AppSetting,
    CandidateAddress,
    CandidateProfile,
    CrawlRun,
    FeedbackEvent,
    JobPosting,
    PreferenceProfile,
    Source,
)
from jobradar.services import jobs, profile, settings
from jobradar.services.crawl_reporting import firecrawl_runtime_status


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


def add_source(session: Session) -> Source:
    source = Source(
        name="Bundesagentur für Arbeit",
        slug="ba",
        kind="api",
        enabled=True,
        is_official=False,
        free_api=True,
    )
    session.add(source)
    session.commit()
    return source


def job_values(*, description: str | None = None) -> dict[str, object]:
    return {
        "title": "Assistenz der Geschäftsführung (m/w/d)",
        "employer": "Beispiel GmbH",
        "description_text": description
        or (
            "Sie unterstützen die Geschäftsführung, koordinieren Termine und Korrespondenz, "
            "bereiten Meetings sowie Präsentationen vor und organisieren Geschäftsreisen."
        ),
        "location_text": "Stuttgart",
        "city": "Stuttgart",
        "state": "Baden-Württemberg",
        "remote_type": "onsite",
        "employment_type": "Vollzeit",
        "contract_type": "unbefristet",
        "language": "de",
        "structured_data": {
            "required_skills": ["Organisation", "PowerPoint"],
            "commute_minutes": 55,
        },
    }


def test_job_upsert_retains_only_changed_snapshots(session: Session) -> None:
    source = add_source(session)
    job, created, changed = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1000",
        values=job_values(),
    )
    assert (created, changed) == (True, True)
    assert len(jobs.require_job(session, job.id).snapshots) == 1

    _, created, changed = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1000",
        values=job_values(),
    )
    assert (created, changed) == (False, False)
    assert len(jobs.require_job(session, job.id).snapshots) == 1

    values = job_values(description="Geschäftsführung, Kalender und neue Aufgaben")
    _, created, changed = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1000",
        values=values,
    )
    assert (created, changed) == (False, True)
    snapshots = jobs.require_job(session, job.id).snapshots
    assert len(snapshots) == 2
    assert snapshots[-1].change_kind == "changed"

    _, created, changed = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1000",
        values=job_values(),
    )
    assert (created, changed) == (False, True)
    assert len(jobs.require_job(session, job.id).snapshots) == 2


def test_scores_are_separate_and_reasons_are_transparent(session: Session) -> None:
    source = add_source(session)
    job, _, _ = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1001",
        values=job_values(),
    )
    candidate = CandidateProfile(
        structured_data={"skills": ["Organisation", "PowerPoint"]},
        is_confirmed=True,
    )
    preference = PreferenceProfile(
        candidate=candidate,
        employment_types=["Vollzeit"],
        contract_types=["unbefristet"],
        languages=["de", "en"],
        onsite_weight=1.0,
        hybrid_weight=0.5,
        remote_weight=0.2,
    )
    session.add_all([candidate, preference])
    session.commit()

    score = jobs.score_job(session, job, candidate=candidate, preferences=preference)
    assert score.relevance_score >= 70
    assert score.candidate_fit_score is not None
    assert score.candidate_fit_score >= 90
    assert score.relevance_reasons
    assert score.fit_reasons
    assert all("explanation" in reason for reason in score.relevance_reasons)
    assert score.relevance_score != score.candidate_fit_score


def test_missing_salary_is_neutral() -> None:
    job = JobPosting(
        source_id=1,
        external_id="x",
        title="Assistenz der Geschäftsführung",
        description_text="Unterstützung der Geschäftsführung und Terminmanagement",
        employment_type=None,
        contract_type=None,
        language=None,
        remote_type=None,
        structured_data={},
    )
    preference = PreferenceProfile(min_salary=55_000, target_salary=65_000)
    fit = score_candidate_fit(job, None, preference)
    salary_reason = next(reason for reason in fit.reasons if reason.code == "salary")
    assert salary_reason.effect == "neutral"
    assert salary_reason.possible_points == 0
    assert fit.value > 0

def test_extended_preferences_add_explainable_fit_dimensions() -> None:
    job = JobPosting(
        source_id=1,
        external_id="extended-preferences",
        title="Executive Assistant",
        description_text="Unterstützung der Geschäftsführung",
        remote_type="onsite",
        structured_data={
            "industry": "Technologie",
            "company_size": "200-1000",
            "travel_percent": 5,
        },
    )
    preference = PreferenceProfile(
        extra_preferences={
            "industries": ["Technologie"],
            "company_sizes": ["200-1000"],
            "max_travel_percent": 10,
        }
    )

    fit = score_candidate_fit(job, None, preference)
    reasons = {reason.code: reason for reason in fit.reasons}

    assert reasons["industry"].effect == "positive"
    assert reasons["company_size"].effect == "positive"
    assert reasons["travel"].effect == "positive"
    assert reasons["industry"].possible_points == 10
    assert reasons["company_size"].possible_points == 5
    assert reasons["travel"].possible_points == 5



def test_title_is_not_the_only_relevance_signal() -> None:
    matching_tasks = JobPosting(
        source_id=1,
        external_id="a",
        title="Office Manager",
        description_text=(
            "Direkte Unterstützung der Geschäftsführung, Terminkoordination, Korrespondenz, "
            "Meeting-Protokolle und Präsentationen"
        ),
        structured_data={},
    )
    misleading_title = JobPosting(
        source_id=1,
        external_id="b",
        title="Assistenz der Geschäftsführung",
        description_text="Aushilfe ohne weitere Angaben",
        structured_data={},
    )
    assert score_role_relevance(matching_tasks).value > score_role_relevance(misleading_title).value


def test_saved_additional_role_title_affects_local_role_scoring() -> None:
    job = JobPosting(
        source_id=1,
        external_id="role-taxonomy",
        title="Executive Office Coordinator",
        description_text="Allgemeine Koordination ohne weitere Angaben.",
        structured_data={},
    )
    matching = PreferenceProfile(
        primary_role_terms=["Executive Assistant"],
        additional_role_terms=["Executive Office Coordinator"],
    )
    baseline = PreferenceProfile(
        primary_role_terms=["Executive Assistant"],
        additional_role_terms=[],
    )

    matching_score, _ = evaluate_job(job, preferences=matching)
    baseline_score, _ = evaluate_job(job, preferences=baseline)

    assert matching_score.value > baseline_score.value
    title_reason = next(reason for reason in matching_score.reasons if reason.code == "role_title")
    assert title_reason.evidence == ("Executive Office Coordinator",)


def test_status_is_idempotent_and_records_feedback(session: Session) -> None:
    source = add_source(session)
    job, _, _ = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1002",
        values=job_values(),
    )
    first = jobs.update_application_status(
        session, job.id, ApplicationStatus.SAVED, notes="Interessant"
    )
    second = jobs.update_application_status(session, job.id, ApplicationStatus.SAVED)
    assert first.id == second.id
    assert second.status == "Merkliste"
    assert session.query(FeedbackEvent).count() == 1

    applied = jobs.update_application_status(session, job.id, ApplicationStatus.APPLIED)
    applied_again = jobs.update_application_status(session, job.id, ApplicationStatus.APPLIED)
    assert applied.id == applied_again.id
    assert applied_again.applied_at == applied.applied_at


def test_profile_update_requests_background_rescore(session: Session) -> None:
    candidate = profile.update_candidate_profile(
        session,
        {
            "full_name": "Ada Beispiel",
            "structured_data": {"skills": ["Organisation"]},
            "is_confirmed": True,
        },
    )
    assert candidate.version == 1
    updated = profile.update_candidate_profile(session, {"full_name": "Ada B. Beispiel"})
    assert updated.version == 2
    request = session.get(AppSetting, "scoring.rescore_request")
    assert request is not None
    assert request.value_json["profile_version"] == 2
    assert request.value_json["status"] == "pending"


def test_candidate_profile_stores_ordered_start_addresses(session: Session) -> None:
    candidate = profile.update_candidate_profile(
        session,
        {
            "addresses": [
                "  Musterstraße 1  ",
                "Zweitweg 2",
                "MUSTERSTRASSE 1",
                "",
            ]
        },
    )

    assert candidate.address_values == ["Musterstraße 1", "Zweitweg 2"]
    assert candidate.home_location == "Musterstraße 1"
    assert [address.position for address in candidate.addresses] == [0, 1]

    updated = profile.update_candidate_profile(
        session, {"addresses": ["Zweitweg 2", "Drittweg 3"]}
    )
    assert updated.address_values == ["Zweitweg 2", "Drittweg 3"]
    assert updated.home_location == "Zweitweg 2"
    assert updated.version == 2

    with pytest.raises(ValueError, match="höchstens 10"):
        profile.update_candidate_profile(
            session, {"addresses": [f"Adresse {index}" for index in range(11)]}
        )
    assert profile.get_candidate_profile(session).version == 2

    legacy_update = profile.update_candidate_profile(
        session, {"home_location": "Kompatibilitätsweg 4"}
    )
    assert legacy_update.address_values == ["Kompatibilitätsweg 4"]
    session.expire_all()
    assert session.query(CandidateAddress).count() == 1
    assert profile.get_candidate_profile(session).address_values == ["Kompatibilitätsweg 4"]


def test_api_gate_and_separate_firecrawl_permission(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings,
        "get_runtime_settings",
        lambda: Settings(crawling_enabled=False, firecrawl_enabled=True),
    )
    with pytest.raises(settings.CrawlingLockedError):
        settings.set_crawling_enabled(session, True)
    with pytest.raises(settings.CrawlingLockedError):
        settings.set_firecrawl_enabled(session, True)
    assert settings.is_crawling_enabled(session) is False
    assert settings.is_firecrawl_enabled(session) is False

    monkeypatch.setattr(
        settings,
        "get_runtime_settings",
        lambda: Settings(crawling_enabled=True, firecrawl_enabled=False),
    )
    settings.set_crawling_enabled(session, True)
    with pytest.raises(settings.CrawlingLockedError):
        settings.set_firecrawl_enabled(session, True)
    assert settings.is_crawling_enabled(session) is True
    assert settings.is_firecrawl_enabled(session) is False

    monkeypatch.setattr(
        settings,
        "get_runtime_settings",
        lambda: Settings(crawling_enabled=True, firecrawl_enabled=True),
    )
    settings.set_firecrawl_enabled(session, True)
    assert settings.is_crawling_enabled(session) is True
    assert settings.is_firecrawl_enabled(session) is True


def test_settings_api_toggles_firecrawl_without_disabling_apis(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Settings(crawling_enabled=True, firecrawl_enabled=True)
    monkeypatch.setattr(settings, "get_runtime_settings", lambda: runtime)
    monkeypatch.setattr("jobradar.api.get_runtime_settings", lambda: runtime)
    app = FastAPI()
    app.include_router(router)

    def override_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    api_enabled = client.put("/settings", json={"api_access_enabled": True})
    assert api_enabled.status_code == 200
    assert api_enabled.json()["api_access_enabled"] is True
    assert api_enabled.json()["firecrawl_enabled"] is False

    firecrawl_enabled = client.put("/settings", json={"firecrawl_enabled": True})
    assert firecrawl_enabled.status_code == 200
    assert firecrawl_enabled.json()["api_access_enabled"] is True
    assert firecrawl_enabled.json()["firecrawl_enabled"] is True

    firecrawl_disabled = client.put("/settings", json={"firecrawl_enabled": False})
    assert firecrawl_disabled.status_code == 200
    assert firecrawl_disabled.json()["api_access_enabled"] is True
    assert firecrawl_disabled.json()["firecrawl_enabled"] is False


def test_stale_running_crawler_is_reported_as_error(session: Session) -> None:
    source = Source(
        name="Firecrawl",
        slug="firecrawl_self_hosted",
        kind="crawler",
        enabled=True,
        metadata_json={
            "firecrawl": {
                "targets": [
                    {
                        "url": "https://portal.example/jobs",
                        "target_kind": "job_portal",
                    }
                ]
            }
        },
    )
    session.add(source)
    session.flush()
    now = datetime(2026, 7, 24, 12, tzinfo=UTC)
    session.add(
        CrawlRun(
            source_id=source.id,
            status="running",
            started_at=now - timedelta(hours=4),
        )
    )
    session.commit()

    runtime = firecrawl_runtime_status(session, source, enabled=True, now=now)

    assert runtime.state == "error"
    assert runtime.error_message is not None

def test_partial_crawler_run_is_reported_as_limited(session: Session) -> None:
    source = Source(
        name="Firecrawl",
        slug="firecrawl_self_hosted",
        kind="crawler",
        enabled=True,
        metadata_json={
            "firecrawl": {
                "targets": [
                    {
                        "url": "https://portal.example/jobs",
                        "target_kind": "job_portal",
                    }
                ]
            }
        },
    )
    session.add(source)
    session.flush()
    finished_at = datetime(2026, 7, 24, 12, tzinfo=UTC)
    session.add(
        CrawlRun(
            source_id=source.id,
            status="partial",
            started_at=finished_at - timedelta(minutes=5),
            finished_at=finished_at,
            error_count=1,
            error_message="Rate-Limit: portal.example",
        )
    )
    session.commit()

    runtime = firecrawl_runtime_status(session, source, enabled=True)

    assert runtime.state == "limited"
    assert runtime.error_message == "Rate-Limit: portal.example"


def test_health_splits_job_portal_and_company_crawler_activity(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Settings(crawling_enabled=True, firecrawl_enabled=True)
    monkeypatch.setattr(settings, "get_runtime_settings", lambda: runtime)
    monkeypatch.setattr("jobradar.api.get_runtime_settings", lambda: runtime)
    session.add_all(
        [
            AppSetting(key=settings.CRAWLING_SETTING_KEY, value_json=True),
            AppSetting(key=settings.FIRECRAWL_SETTING_KEY, value_json=True),
        ]
    )
    source = Source(
        name="Firecrawl",
        slug="firecrawl_self_hosted",
        kind="crawler",
        enabled=True,
        metadata_json={
            "firecrawl": {
                "targets": [
                    {
                        "url": "https://portal.example/jobs",
                        "label": "Portal",
                        "target_kind": "job_portal",
                    },
                    {
                        "url": "https://company.example/careers",
                        "label": "Company",
                        "target_kind": "company_site",
                    },
                ]
            }
        },
    )
    session.add(source)
    session.commit()
    for external_id, label, target_kind in (
        ("portal-1", "Portal", "job_portal"),
        ("company-1", "Company", "company_site"),
    ):
        values = job_values()
        values["structured_data"] = {
            "source_metadata": {
                "firecrawl_target_label": label,
                "firecrawl_target_kind": target_kind,
            }
        }
        job, _, _ = jobs.upsert_job(
            session,
            source_id=source.id,
            external_id=external_id,
            values=values,
        )
        if target_kind == "company_site":
            job.created_at = datetime(2026, 7, 20, tzinfo=UTC)
    finished_at = datetime(2026, 7, 23, 18, 22, tzinfo=UTC)
    running_at = datetime.now(UTC)
    session.add(
        CrawlRun(
            source_id=source.id,
            status="succeeded",
            started_at=finished_at,
            finished_at=finished_at,
            details={
                "connector": {
                    "target_results": [
                        {
                            "target": "https://portal.example/jobs",
                            "status": "completed",
                            "records_emitted": 1,
                        },
                        {
                            "target": "https://company.example/careers",
                            "status": "completed",
                            "records_emitted": 1,
                        },
                    ]
                }
            },
        )
    )
    session.add(
        CrawlRun(
            source_id=source.id,
            status="running",
            started_at=running_at,
        )
    )
    session.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    payload = TestClient(app).get("/health").json()

    assert payload["job_portal_crawling"] == {
        "active": True,
        "running": True,
        "state": "running",
        "job_count": 1,
        "added_last_day": 1,
        "last_fetched_at": finished_at.isoformat().replace("+00:00", "Z"),
    }
    assert payload["company_site_crawling"] == {
        "active": True,
        "running": True,
        "state": "running",
        "job_count": 1,
        "added_last_day": 0,
        "last_fetched_at": finished_at.isoformat().replace("+00:00", "Z"),
    }
    assert "web_crawling" not in payload


def test_health_does_not_report_failed_enabled_crawler_as_active(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Settings(crawling_enabled=True, firecrawl_enabled=True)
    monkeypatch.setattr(settings, "get_runtime_settings", lambda: runtime)
    monkeypatch.setattr("jobradar.api.get_runtime_settings", lambda: runtime)
    session.add_all(
        [
            AppSetting(key=settings.CRAWLING_SETTING_KEY, value_json=True),
            AppSetting(key=settings.FIRECRAWL_SETTING_KEY, value_json=True),
        ]
    )
    source = Source(
        name="Firecrawl",
        slug="firecrawl_self_hosted",
        kind="crawler",
        enabled=True,
        status="error",
        last_error="ConnectError: connection refused",
        metadata_json={
            "firecrawl": {
                "targets": [
                    {
                        "url": "https://portal.example/jobs",
                        "label": "Portal",
                        "target_kind": "job_portal",
                    }
                ]
            }
        },
    )
    session.add(source)
    session.flush()
    failed_at = datetime.now(UTC)
    session.add(
        CrawlRun(
            source_id=source.id,
            status="failed",
            started_at=failed_at,
            finished_at=failed_at,
            error_count=1,
            error_message="ConnectError: connection refused",
            details={
                "connector": {
                    "target_results": [
                        {
                            "target": "https://portal.example/jobs",
                            "status": "failed",
                        }
                    ]
                }
            },
        )
    )
    session.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    activity = TestClient(app).get("/health").json()["job_portal_crawling"]

    assert activity["active"] is False
    assert activity["running"] is False
    assert activity["state"] == "error"


def test_api_lists_jobs_and_does_not_offer_crawl_trigger(session: Session) -> None:
    source = add_source(session)
    job, _, _ = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id="1003",
        values=job_values(),
    )
    jobs.score_job(session, job)
    last_crawl_at = datetime(2026, 7, 22, 18, 22, tzinfo=UTC)
    source.last_success_at = last_crawl_at
    session.add(
        Source(
            name="Firecrawl",
            slug="firecrawl_self_hosted",
            kind="crawler",
            enabled=True,
            is_official=False,
            free_api=True,
        )
    )
    session.commit()

    app = FastAPI()
    app.include_router(router)

    def override_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload["firecrawl_enabled"] is False
    assert health_payload["api_access_enabled"] is False
    assert "crawling_enabled" not in health_payload
    assert health_payload["api_calls"]["job_count"] == 1
    assert health_payload["api_calls"]["running"] is False
    assert health_payload["api_calls"]["added_last_day"] == 1
    assert (
        datetime.fromisoformat(health_payload["api_calls"]["last_fetched_at"])
        == last_crawl_at
    )
    assert health_payload["job_portal_crawling"] == {
        "active": False,
        "running": False,
        "state": "paused",
        "job_count": 0,
        "added_last_day": 0,
        "last_fetched_at": None,
    }
    assert health_payload["company_site_crawling"] == {
        "active": False,
        "running": False,
        "state": "paused",
        "job_count": 0,
        "added_last_day": 0,
        "last_fetched_at": None,
    }
    response = client.get("/jobs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["relevance_score"] is not None

    detail = client.get(f"/jobs/{job.id}")
    assert detail.status_code == 200
    assert detail.json()["latest_score"]["relevance_reasons"]

    status_response = client.patch(f"/jobs/{job.id}/status", json={"status": "Merkliste"})
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "Merkliste"

    assert client.post("/crawl-runs").status_code == 405


def test_preference_api_exposes_editable_role_taxonomy(session: Session) -> None:
    app = FastAPI()
    app.include_router(router)

    def override_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    response = client.put(
        "/preferences",
        json={
            "primary_role_terms": ["Executive Assistant", "Chief of Staff"],
            "additional_role_terms": ["Executive Office Coordinator"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["primary_role_terms"] == ["Executive Assistant", "Chief of Staff"]
    assert payload["additional_role_terms"] == ["Executive Office Coordinator"]
    assert "role_terms" not in payload
