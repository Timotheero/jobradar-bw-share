from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from jobradar.config import Settings
from jobradar.db import Base, build_engine, get_db
from jobradar.models import AppSetting, CrawlRun, Source
from jobradar.services import settings as setting_service
from jobradar.services.sync import ConnectorFactory, ensure_default_sources
from jobradar.web import router


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.settings = Settings(default_locale="de")
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
    app.include_router(router)

    def override_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client


def test_default_catalog_keeps_ba_and_registers_no_signup_sources(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        sources = {source.slug: source for source in ensure_default_sources(session)}

        assert sources["ba_jobsuche"].enabled is True
        assert sources["ba_jobsuche"].metadata_json["authorization"] == (
            "written_permission_confirmed"
        )
        assert {
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
            "personio",
            "arbeitnow",
            "jobicy_remote",
            "remotive_remote",
        } <= set(sources)
        assert all(
            sources[slug].enabled
            for slug in (
                "greenhouse",
                "lever",
                "ashby",
                "smartrecruiters",
                "personio",
                "arbeitnow",
                "jobicy_remote",
                "remotive_remote",
            )
        )

        connector = ConnectorFactory.with_defaults().create(sources["greenhouse"])
        assert connector is not None
        assert connector.run_diagnostics["skipped"] is True
        assert connector.run_diagnostics["skip_reason"] == "no_targets"


def test_public_board_form_normalizes_url_and_saves_without_fetching(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/sources/public-board",
        data={
            "provider": "greenhouse",
            "board": "https://boards.greenhouse.io/acme/jobs/123",
            "company": "Acme GmbH",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/sources?board_added=true")
    with session_factory() as session:
        source = session.scalar(select(Source).where(Source.slug == "greenhouse"))
        assert source is not None
        assert source.enabled is True
        assert source.last_success_at is None
        assert source.metadata_json["boards"] == [
            {"identifier": "acme", "company": "Acme GmbH"}
        ]

    rendered = client.get("/sources")
    assert rendered.status_code == 200
    assert "Acme GmbH" in rendered.text
    assert "Arbeitgeber-Feed wurde gespeichert" not in rendered.text

    duplicate = client.post(
        "/sources/public-board",
        data={
            "provider": "greenhouse",
            "board": "acme",
            "company": "Anderer Name",
        },
        follow_redirects=False,
    )
    assert duplicate.status_code == 303
    with session_factory() as session:
        source = session.scalar(select(Source).where(Source.slug == "greenhouse"))
        assert source is not None
        assert len(source.metadata_json["boards"]) == 1


def test_public_board_form_rejects_cross_provider_urls(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/sources/public-board",
        data={
            "provider": "greenhouse",
            "board": "https://jobs.lever.co/acme",
            "company": "Acme GmbH",
        },
    )

    assert response.status_code == 422
    assert "gültige Board-Kennung" in response.json()["detail"]
    with session_factory() as session:
        source = session.scalar(select(Source).where(Source.slug == "greenhouse"))
        assert source is None


def test_firecrawl_targets_are_categorized_and_rendered_without_fetching(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    portal = client.post(
        "/sources/firecrawl-target",
        data={
            "url": "https://www.stepstone.de/jobs/assistenz",
            "target_kind": "job_portal",
        },
        follow_redirects=False,
    )
    company = client.post(
        "/sources/firecrawl-target",
        data={
            "url": "https://example.com/careers/jobs",
            "target_kind": "company_site",
        },
        follow_redirects=False,
    )

    assert portal.status_code == 303
    assert company.status_code == 303
    with session_factory() as session:
        source = session.scalar(
            select(Source).where(Source.slug == "firecrawl_self_hosted")
        )
        assert source is not None
        assert source.last_success_at is None
        assert source.metadata_json["firecrawl"]["targets"] == [
            {
                "url": "https://www.stepstone.de/jobs/assistenz",
                "target_kind": "job_portal",
            },
            {
                "url": "https://example.com/careers/jobs",
                "target_kind": "company_site",
            },
        ]
        assert "targets" not in source.metadata_json
        finished_at = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)
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
                                "target": "https://www.stepstone.de/jobs/assistenz",
                                "target_kind": "job_portal",
                                "status": "completed",
                                "records_emitted": 7,
                            },
                            {
                                "target": "https://example.com/careers/jobs",
                                "target_kind": "company_site",
                                "status": "completed",
                                "records_emitted": 2,
                            },
                        ]
                    }
                },
            )
        )
        session.commit()

    rendered = client.get("/sources")
    assert rendered.status_code == 200
    assert 'id="tab-source-api"' in rendered.text
    assert 'id="tab-source-crawl"' in rendered.text
    assert "StepStone" in rendered.text
    assert "example.com" in rendered.text
    assert 'data-crawl-filter="job_portal"' in rendered.text
    assert 'data-crawl-filter="company_site"' in rendered.text
    assert "BA Jobsuche bleibt eine API-Quelle und wird nicht gecrawlt." in rendered.text
    assert "23.07.2026" in rendered.text
    assert "7 Stellen" in rendered.text
    assert "2 Stellen" in rendered.text
    assert "Zuletzt gecrawlt" in rendered.text
    assert "Im letzten Crawl gefunden" in rendered.text
    assert "Dauerhaften Live-Crawler aktivieren" in rendered.text


def test_firecrawl_toggle_changes_only_crawler_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Settings(crawling_enabled=True, firecrawl_enabled=True)
    monkeypatch.setattr("jobradar.web.get_settings", lambda: runtime)
    monkeypatch.setattr(setting_service, "get_runtime_settings", lambda: runtime)
    with session_factory() as session:
        session.add(
            AppSetting(
                key=setting_service.CRAWLING_SETTING_KEY,
                value_json=True,
            )
        )
        session.commit()

    enabled = client.post(
        "/sources/firecrawl-toggle",
        data={"crawler_enabled": "on"},
        follow_redirects=False,
    )
    assert enabled.status_code == 303
    assert enabled.headers["location"].endswith("/sources?crawler_updated=true")
    with session_factory() as session:
        assert setting_service.database_crawling_switch(session) is True
        assert setting_service.database_firecrawl_switch(session) is True

    disabled = client.post(
        "/sources/firecrawl-toggle",
        data={},
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    with session_factory() as session:
        assert setting_service.database_crawling_switch(session) is True
        assert setting_service.database_firecrawl_switch(session) is False


def test_firecrawl_target_form_rejects_unknown_target_kind(client: TestClient) -> None:
    response = client.post(
        "/sources/firecrawl-target",
        data={
            "url": "https://example.com/jobs",
            "target_kind": "automatic_discovery",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Unbekannte Crawl-Zielart."
