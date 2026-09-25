from __future__ import annotations

import gzip
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.config import Settings  # noqa: E402
from jobradar.connectors.base import (  # noqa: E402
    JobQuery,
    RawJobRecord,
    SourceInfo,
)
from jobradar.db import Base, build_engine  # noqa: E402
from jobradar.integrations.codex.provider import CodexJobSummary  # noqa: E402
from jobradar.models import (  # noqa: E402
    AppSetting,
    CrawlRun,
    CrawlRunStatus,
    JobPosting,
    JobScore,
    JobSnapshot,
    JobSummary,
    Source,
)
from jobradar.services.settings import (  # noqa: E402
    CRAWLING_SETTING_KEY,
    FIRECRAWL_SETTING_KEY,
)
from jobradar.services.sync import (  # noqa: E402
    SyncQueryPlan,
    ensure_default_sources,
    run_all_enabled,
    run_source,
    run_source_by_slug,
)
from jobradar.worker import run_worker  # noqa: E402


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as value:
        yield value


class ExplodingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, _source: Source):
        self.calls += 1
        raise AssertionError("The connector factory crossed the crawling gate")


class FakeBAConnector:
    source_info = SourceInfo(
        key="ba_jobsuche",
        name="Fake BA",
        acquisition="test",
        official=False,
    )

    def __init__(self, record: RawJobRecord) -> None:
        self.record = record
        self.queries: list[JobQuery] = []
        self.closed = False

    def iter_jobs(self, query: JobQuery | None = None):
        assert query is not None
        self.queries.append(query)
        yield self.record

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, connector: FakeBAConnector) -> None:
        self.connector = connector
        self.calls: list[str] = []

    def create(self, source: Source):
        self.calls.append(source.slug)
        if source.slug == "ba_jobsuche":
            return self.connector
        if source.slug == "firecrawl_self_hosted":
            return EmptyConnector()
        return None


class EmptyConnector:
    source_info = SourceInfo(
        key="firecrawl_self_hosted",
        name="Fake Firecrawl",
        acquisition="test",
        official=False,
    )

    def iter_jobs(self, query: JobQuery | None = None):
        del query
        return iter(())


class DiagnosticErrorConnector(EmptyConnector):
    @property
    def run_diagnostics(self):
        return {
            "target_count": 2,
            "target_error_count": 1,
            "target_errors": [{"error": "Timeout: Zielseite nicht erreichbar"}],
            "skipped": False,
        }

class PartialDiagnosticConnector(DiagnosticErrorConnector):
    def iter_jobs(self, query: JobQuery | None = None):
        del query
        yield _record()


def _allow_in_database(session: Session, *, firecrawl: bool = False) -> None:
    session.add(
        AppSetting(
            key=CRAWLING_SETTING_KEY,
            value_json=True,
            description="test-only explicit API activation",
        )
    )
    if firecrawl:
        session.add(
            AppSetting(
                key=FIRECRAWL_SETTING_KEY,
                value_json=True,
                description="test-only explicit Firecrawl activation",
            )
        )
    session.commit()


def _record(description: str = "<p>Direkte <b>Unterstuetzung</b> der Geschaeftsfuehrung</p>"):
    return RawJobRecord(
        source="ba_jobsuche",
        source_job_id="BA-100",
        title="Office Manager",
        company="Beispiel GmbH",
        locations=("70173 Stuttgart, Baden-Wuerttemberg",),
        description=description,
        description_format="html",
        canonical_url="https://example.test/jobs/BA-100",
        apply_url="https://example.test/apply/BA-100",
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 16, tzinfo=UTC),
        employment_types=("VOLLZEIT", "HEIM_TELEARBEIT"),
        remote=True,
        salary="60.000 EUR",
        metadata={"contract_type": "unbefristet", "provider": "fake"},
        raw={"html": description, "providerInternal": {"number": 100}},
    )


def test_runtime_gate_blocks_factory_even_when_database_allows(session: Session) -> None:
    _allow_in_database(session)
    factory = ExplodingFactory()

    result = run_all_enabled(
        session,
        settings=Settings(crawling_enabled=False),
        connector_factory=factory,
        run_type="test",
    )

    assert result.blocked is True
    assert factory.calls == 0
    assert session.query(JobPosting).count() == 0
    blocked = session.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()))
    assert blocked is not None
    assert blocked.status == CrawlRunStatus.BLOCKED.value
    assert blocked.details["network_called"] is False
    assert "CRAWLING_ENABLED" in (blocked.lock_reason or "")


def test_database_gate_is_a_second_network_lock(session: Session) -> None:
    factory = ExplodingFactory()

    result = run_all_enabled(
        session,
        settings=Settings(crawling_enabled=True),
        connector_factory=factory,
        run_type="test",
    )

    assert result.blocked is True
    assert factory.calls == 0
    assert "Anwendungsschalter" in (result.lock_reason or "")


def test_manual_slug_run_checks_gate_before_factory(session: Session) -> None:
    _allow_in_database(session)
    factory = ExplodingFactory()

    run = run_source_by_slug(
        session,
        "firecrawl_self_hosted",
        settings=Settings(crawling_enabled=False),
        connector_factory=factory,
        run_type="test",
    )

    assert run.status == CrawlRunStatus.BLOCKED.value
    assert factory.calls == 0


def test_direct_source_run_cannot_bypass_runtime_gate(session: Session) -> None:
    _allow_in_database(session)
    source = next(
        source for source in ensure_default_sources(session) if source.slug == "ba_jobsuche"
    )
    connector = FakeBAConnector(_record())

    run = run_source(
        session,
        source,
        connector,
        settings=Settings(crawling_enabled=False),
        run_type="test",
    )

    assert run.status == CrawlRunStatus.BLOCKED.value
    assert connector.queries == []
    assert connector.closed is False


def test_default_sources_enable_ba_and_prepare_self_hosted_firecrawl(session: Session) -> None:
    sources = {source.slug: source for source in ensure_default_sources(session)}

    assert sources["ba_jobsuche"].enabled is True
    assert sources["ba_jobsuche"].metadata_json["experimental"] is True
    assert sources["ba_jobsuche"].free_api is True
    assert sources["firecrawl_self_hosted"].enabled is True
    assert sources["firecrawl_self_hosted"].metadata_json["self_hosted"] is True
    assert "api.firecrawl.dev" not in (sources["firecrawl_self_hosted"].base_url or "")
    assert sources["firecrawl_self_hosted"].metadata_json["firecrawl"]["portal_search"] == {
        "enabled": True,
        "limit": 100,
        "accept_external_results": True,
    }


def test_firecrawl_defaults_preserve_targets_and_enable_portal_search(session: Session) -> None:
    source = next(
        source
        for source in ensure_default_sources(session)
        if source.slug == "firecrawl_self_hosted"
    )
    source.metadata_json = {
        **dict(source.metadata_json),
        "firecrawl": {
            "targets": [
                {
                    "url": "https://jobs.example.test/search",
                    "label": "Approved portal",
                    "target_kind": "job_portal",
                }
            ],
            "portal_search": {"limit": 6, "domain_batch_size": 10},
        },
    }
    session.commit()

    migrated = next(
        source
        for source in ensure_default_sources(session)
        if source.slug == "firecrawl_self_hosted"
    )

    assert migrated.metadata_json["firecrawl"]["targets"][0]["label"] == "Approved portal"
    assert migrated.metadata_json["firecrawl"]["portal_search"] == {
        "enabled": True,
        "limit": 100,
        "accept_external_results": True,
    }


def test_firecrawl_query_plan_batches_role_terms_for_local_and_remote_search(
    session: Session,
) -> None:
    source = next(
        source
        for source in ensure_default_sources(session)
        if source.slug == "firecrawl_self_hosted"
    )

    queries = SyncQueryPlan().queries_for(source)

    assert len(queries) == 4
    assert all(query.text and " OR " in query.text for query in queries)
    assert {query.location for query in queries} == {"Baden-Württemberg", "Deutschland"}
    assert sum(bool(query.filters) for query in queries) == 2
    assert all(
        query.filters == {"homeoffice": "nv_true"}
        for query in queries
        if query.location == "Deutschland"
    )


def test_default_ba_query_plan_uses_current_homeoffice_filter_and_role_terms(
    session: Session,
) -> None:
    source = next(
        source for source in ensure_default_sources(session) if source.slug == "ba_jobsuche"
    )

    queries = SyncQueryPlan().queries_for(source)

    assert len(queries) == 32
    assert {query.text for query in queries} == {
        "Assistenz der Geschäftsführung",
        "Executive Assistant",
        "Assistenz",
        "Assistant",
        "Geschäftsführung",
        "Geschäftsleitung",
        "Referent",
        "Office Manager",
        "Chief of Staff",
        "CEO Office",
        "Projektkoordination",
        "Stabsmitarbeiter",
        "Vorstandsassistenz",
        "Executive Office",
        "Management Coordinator",
        "Referent des Vorstands",
    }
    remote_queries = [query for query in queries if query.location == "Deutschland"]
    assert len(remote_queries) == 16
    assert all(query.filters == {"homeoffice": "nv_true"} for query in remote_queries)


def test_source_environment_defaults_apply_only_on_first_registration(
    session: Session,
) -> None:
    runtime = Settings(
        ba_jobs_enabled=False,
        firecrawl_enabled=False,
    )

    sources = {source.slug: source for source in ensure_default_sources(session, settings=runtime)}

    assert sources["ba_jobsuche"].enabled is False
    assert sources["firecrawl_self_hosted"].enabled is False

    sources["ba_jobsuche"].enabled = True
    session.commit()
    ensure_default_sources(session, settings=runtime)
    assert sources["ba_jobsuche"].enabled is True


def test_firecrawl_requires_a_separate_application_permission(session: Session) -> None:
    _allow_in_database(session)
    factory = ExplodingFactory()

    run = run_source_by_slug(
        session,
        "firecrawl_self_hosted",
        settings=Settings(crawling_enabled=True, firecrawl_enabled=True),
        connector_factory=factory,
        run_type="test",
    )

    assert run.status == CrawlRunStatus.BLOCKED.value
    assert factory.calls == 0
    assert "separate Firecrawl-Freigabe" in (run.lock_reason or "")


def test_firecrawl_without_targets_is_a_network_silent_prepared_run(session: Session) -> None:
    _allow_in_database(session, firecrawl=True)

    run = run_source_by_slug(
        session,
        "firecrawl_self_hosted",
        settings=Settings(crawling_enabled=True),
        run_type="test",
    )

    assert run.status == CrawlRunStatus.SUCCEEDED.value
    assert run.details["connector"]["skipped"] is True
    assert run.details["connector"]["skip_reason"] == "no_targets"
    source = session.scalar(select(Source).where(Source.slug == "firecrawl_self_hosted"))
    assert source is not None
    assert source.status == "prepared"


def test_connector_target_errors_are_visible_in_crawl_run(session: Session) -> None:
    _allow_in_database(session, firecrawl=True)

    run = run_source_by_slug(
        session,
        "firecrawl_self_hosted",
        settings=Settings(crawling_enabled=True),
        connector_factory=lambda _source: DiagnosticErrorConnector(),
        run_type="test",
    )

    assert run.status == CrawlRunStatus.FAILED.value
    assert run.error_count == 1
    assert "Timeout" in (run.error_message or "")
    assert run.details["partial_success"] is False

def test_connector_target_errors_with_records_are_a_partial_run(session: Session) -> None:
    _allow_in_database(session, firecrawl=True)

    run = run_source_by_slug(
        session,
        "firecrawl_self_hosted",
        settings=Settings(crawling_enabled=True),
        connector_factory=lambda _source: PartialDiagnosticConnector(),
        run_type="test",
    )

    assert run.status == CrawlRunStatus.PARTIAL.value
    assert run.fetched_count == 1
    assert run.error_count == 1
    assert run.details["partial_success"] is True
    assert run.source is not None
    assert run.source.status == "limited"
    assert run.source.last_success_at == run.finished_at


def test_enabled_sync_ingests_versions_compressed_raw_and_local_score(session: Session) -> None:
    _allow_in_database(session, firecrawl=True)
    connector = FakeBAConnector(_record())
    factory = FakeFactory(connector)

    result = run_all_enabled(
        session,
        settings=Settings(crawling_enabled=True),
        connector_factory=factory,
        query_plan=SyncQueryPlan(search_terms=(None,), page_size=25, max_pages=2),
        run_type="test",
    )

    assert result.blocked is False
    assert {"ba_jobsuche", "firecrawl_self_hosted"} <= set(factory.calls)
    assert connector.closed is True
    assert len(connector.queries) == 2
    assert connector.queries[0].location == "Baden-Württemberg"
    assert connector.queries[1].location == "Deutschland"
    assert connector.queries[1].filters["homeoffice"] == "nv_true"

    run = next(run for run in result.runs if run.source and run.source.slug == "ba_jobsuche")
    assert run.status == CrawlRunStatus.SUCCEEDED.value
    assert run.discovered_count == 2
    assert run.fetched_count == 1
    assert run.created_count == 1
    assert run.skipped_count == 1

    job = session.scalar(select(JobPosting))
    assert job is not None
    assert job.description_text == "Direkte Unterstuetzung der Geschaeftsfuehrung"
    assert "<" not in job.description_text
    assert job.state == "Baden-Württemberg"
    assert job.remote_type == "hybrid"
    assert job.content_hash
    assert job.availability_status == "active"
    assert job.availability_check_method == "source_sync"
    assert job.availability_reason == "present_in_source"
    assert job.availability_checked_at is not None
    assert session.query(JobScore).count() == 1
    score = session.scalar(select(JobScore))
    assert score is not None
    assert score.relevance_score >= 0
    assert score.candidate_fit_score is not None

    snapshot = session.scalar(select(JobSnapshot))
    assert snapshot is not None
    stored = json.loads(gzip.decompress(snapshot.raw_html_compressed).decode("utf-8"))
    assert stored["raw"]["providerInternal"]["number"] == 100
    assert stored["description"].startswith("<p>")


def test_unchanged_record_does_not_create_duplicate_snapshot_or_score(session: Session) -> None:
    _allow_in_database(session)
    first_connector = FakeBAConnector(_record())
    first = run_all_enabled(
        session,
        settings=Settings(crawling_enabled=True),
        connector_factory=FakeFactory(first_connector),
        query_plan=SyncQueryPlan(search_terms=(None,), include_germany_remote=False),
        run_type="test",
    )
    second_connector = FakeBAConnector(_record())
    second = run_all_enabled(
        session,
        settings=Settings(crawling_enabled=True),
        connector_factory=FakeFactory(second_connector),
        query_plan=SyncQueryPlan(search_terms=(None,), include_germany_remote=False),
        run_type="test",
    )

    assert first.created_count == 1
    ba_run = next(run for run in second.runs if run.source and run.source.slug == "ba_jobsuche")
    assert ba_run.skipped_count == 1
    assert session.query(JobPosting).count() == 1
    assert session.query(JobSnapshot).count() == 1
    assert session.query(JobScore).count() == 1


def test_changed_description_creates_new_snapshot_and_score(session: Session) -> None:
    _allow_in_database(session)
    common = {
        "settings": Settings(crawling_enabled=True),
        "query_plan": SyncQueryPlan(search_terms=(None,), include_germany_remote=False),
        "run_type": "test",
    }
    run_all_enabled(
        session,
        connector_factory=FakeFactory(FakeBAConnector(_record())),
        **common,
    )
    changed = run_all_enabled(
        session,
        connector_factory=FakeFactory(
            FakeBAConnector(_record("<p>Neue Aufgaben: Vorstandstermine und Protokolle</p>"))
        ),
        **common,
    )

    assert changed.updated_count == 1
    assert session.query(JobSnapshot).count() == 2
    assert session.query(JobScore).count() == 2


class BatchSummaryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[list[object], object, dict[str, object]]] = []

    async def summarize_jobs(self, jobs, profile, **options):
        self.calls.append((jobs, profile, options))
        return {
            job.job_id: CodexJobSummary(
                overview=f"Kurzfassung {job.job_id}",
                key_points=("Aufgabe", "Arbeitsmodell", "Vertrag"),
                missing_information=("Vergütung",),
            )
            for job in jobs
        }


def test_worker_precomputes_only_relevant_summaries_in_bounded_batches(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        source = Source(name="Test", slug="summary-test")
        session.add(source)
        session.flush()
        relevant = [
            JobPosting(
                source_id=source.id,
                external_id=f"relevant-{index}",
                title="Assistenz",
                description_text="Direkte Unterstützung der Geschäftsführung",
                content_hash=f"relevant-hash-{index}",
            )
            for index in range(21)
        ]
        weak = JobPosting(
            source_id=source.id,
            external_id="weak",
            title="Sachbearbeitung",
            description_text="Allgemeine Verwaltung",
            content_hash="weak-hash",
        )
        session.add_all((*relevant, weak))
        session.flush()
        session.add_all(
            (
                *(JobScore(job_id=job.id, relevance_score=60) for job in relevant),
                JobScore(job_id=weak.id, relevance_score=90),
                JobScore(job_id=weak.id, relevance_score=59.9),
            )
        )
        session.commit()
        relevant_ids = [job.id for job in relevant]

    provider = BatchSummaryProvider()
    options = {
        "session_factory": session_factory,
        "settings": Settings(
            crawling_enabled=False,
            codex_enabled=True,
            codex_max_jobs_per_run=50,
        ),
        "connector_factory": ExplodingFactory(),
        "summary_provider": provider,
        "interval_minutes": 360,
        "once": True,
    }
    run_worker(**options)
    run_worker(**options)

    assert [len(jobs) for jobs, _profile, _options in provider.calls] == [20, 1]
    generated_ids = [
        int(job.job_id)
        for jobs, _profile, _options in provider.calls
        for job in jobs
    ]
    assert generated_ids == relevant_ids
    assert all(
        call_options
        == {
            "locale": "de",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
        }
        for _jobs, _profile, call_options in provider.calls
    )
    with session_factory() as session:
        summaries = session.scalars(select(JobSummary)).all()
        assert len(summaries) == 21
        assert {summary.job_id for summary in summaries} == set(relevant_ids)


def test_worker_once_keeps_disabled_configuration_network_silent(
    session_factory: sessionmaker[Session],
) -> None:
    factory = ExplodingFactory()

    run_worker(
        session_factory=session_factory,
        settings=Settings(crawling_enabled=False),
        connector_factory=factory,
        interval_minutes=360,
        once=True,
    )

    assert factory.calls == 0
    with session_factory() as session:
        run = session.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()))
        assert run is not None
        assert run.status == CrawlRunStatus.BLOCKED.value


def test_worker_source_mode_runs_only_the_requested_connector(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _allow_in_database(session, firecrawl=True)

    factory = FakeFactory(FakeBAConnector(_record()))
    run_worker(
        session_factory=session_factory,
        settings=Settings(crawling_enabled=True, firecrawl_enabled=True),
        connector_factory=factory,
        source_slug="firecrawl_self_hosted",
        interval_minutes=120,
        once=True,
    )

    assert factory.calls == ["firecrawl_self_hosted"]
    with session_factory() as session:
        runs = session.scalars(select(CrawlRun).order_by(CrawlRun.id)).all()
        assert len(runs) == 1
        assert runs[0].source is not None
        assert runs[0].source.slug == "firecrawl_self_hosted"
        assert runs[0].run_type == "scheduled"
        assert runs[0].status == CrawlRunStatus.SUCCEEDED.value




def test_batch_excludes_source_owned_by_dedicated_worker(
    session: Session,
) -> None:
    _allow_in_database(session, firecrawl=True)
    factory = FakeFactory(FakeBAConnector(_record()))

    run_all_enabled(
        session,
        settings=Settings(crawling_enabled=True, firecrawl_enabled=True),
        connector_factory=factory,
        exclude_source_slugs=("firecrawl_self_hosted",),
    )

    assert "firecrawl_self_hosted" not in factory.calls
