from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from jobradar.config import Settings
from jobradar.db import Base, build_engine, get_db
from jobradar.models import Source
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


def test_new_source_catalog_is_complete_and_network_silent(
    session_factory: sessionmaker[Session],
) -> None:
    expected = {
        "interamt",
        "deutsche_bahn",
        "join",
        "softgarden",
        "successfactors",
        "workday",
        "workable",
        "recruitee",
        "teamtailor",
        "icims",
        "oracle",
        "phenom",
        "radancy",
        "beesite",
        "rheinmetall",
        "tkms",
    }
    configurable = expected - {"interamt", "deutsche_bahn", "rheinmetall", "tkms"}

    with session_factory() as session:
        sources = {source.slug: source for source in ensure_default_sources(session)}
        assert expected <= set(sources)
        factory = ConnectorFactory.with_defaults()
        for slug in expected:
            connector = factory.create(sources[slug])
            assert connector is not None
            if slug in configurable:
                assert connector.run_diagnostics["skipped"] is True
                assert connector.run_diagnostics["skip_reason"] == "no_targets"


def test_sources_page_lists_new_providers(client: TestClient) -> None:
    response = client.get("/sources")

    assert response.status_code == 200
    for label in (
        "JOIN",
        "softgarden",
        "SAP SuccessFactors",
        "Workday",
        "Workable",
        "Recruitee",
        "Teamtailor",
        "iCIMS",
        "Oracle Recruiting Cloud",
        "Phenom",
        "Radancy",
        "BeeSite",
    ):
        assert f'>{label}</option>' in response.text


def test_tenant_and_url_boards_are_normalized_without_fetching(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    join_response = client.post(
        "/sources/public-board",
        data={
            "provider": "join",
            "board": "https://join.com/companies/acme",
            "company": "Acme GmbH",
        },
        follow_redirects=False,
    )
    workday_response = client.post(
        "/sources/public-board",
        data={
            "provider": "workday",
            "board": "https://acme.wd5.myworkdayjobs.com/de-DE/External",
            "company": "Acme GmbH",
        },
        follow_redirects=False,
    )

    assert join_response.status_code == 303
    assert workday_response.status_code == 303
    with session_factory() as session:
        sources = {
            source.slug: source
            for source in session.scalars(
                select(Source).where(Source.slug.in_(("join", "workday")))
            )
        }
        assert sources["join"].metadata_json["boards"] == [
            {"identifier": "acme", "company": "Acme GmbH"}
        ]
        assert sources["workday"].metadata_json["boards"] == [
            {
                "identifier": "https://acme.wd5.myworkdayjobs.com/de-DE/External",
                "company": "Acme GmbH",
                "careers_url": "https://acme.wd5.myworkdayjobs.com/de-DE/External",
                "tenant": "acme",
                "site": "External",
            }
        ]


def test_generic_url_provider_rejects_non_public_url(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/sources/public-board",
        data={
            "provider": "beesite",
            "board": "https://localhost/jobs",
            "company": "Local",
        },
    )

    assert response.status_code == 422
    with session_factory() as session:
        assert session.scalar(select(Source).where(Source.slug == "beesite")) is None
