from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from jobradar import cli
from jobradar.config import Settings
from jobradar.db import Base, build_engine
from jobradar.models import CrawlRun, CrawlRunStatus
from jobradar.services import sync


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    yield factory
    engine.dispose()


def _patch_cli_database(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
    *,
    crawling_enabled: bool,
) -> None:
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(crawling_enabled=crawling_enabled),
    )


@pytest.mark.parametrize("source_args", [[], ["--source", "firecrawl_self_hosted"]])
def test_sync_once_stays_network_silent_when_runtime_gate_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session_factory: sessionmaker[Session],
    source_args: list[str],
) -> None:
    _patch_cli_database(
        monkeypatch,
        session_factory,
        crawling_enabled=False,
    )
    factory_calls = 0

    def fail_if_factory_is_built(cls):
        del cls
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("connector factory crossed the CLI crawling gate")

    monkeypatch.setattr(
        sync.ConnectorFactory,
        "with_defaults",
        classmethod(fail_if_factory_is_built),
    )

    exit_code = cli.run(["sync-once", *source_args])

    assert exit_code == 2
    assert factory_calls == 0
    assert "CRAWLING_ENABLED" in capsys.readouterr().out


def test_sync_once_counts_sync_batch_result_without_len_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session_factory: sessionmaker[Session],
) -> None:
    _patch_cli_database(monkeypatch, session_factory, crawling_enabled=True)
    result = sync.SyncBatchResult(
        runs=(
            CrawlRun(status=CrawlRunStatus.SUCCEEDED.value),
            CrawlRun(status=CrawlRunStatus.SUCCEEDED.value),
        )
    )
    monkeypatch.setattr(sync, "run_all_enabled", lambda *args, **kwargs: result)

    exit_code = cli.run(["sync-once"])

    assert exit_code == 0
    assert "2 Quellenlaeufe abgeschlossen." in capsys.readouterr().out


def test_sync_once_handles_single_source_crawl_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session_factory: sessionmaker[Session],
) -> None:
    _patch_cli_database(monkeypatch, session_factory, crawling_enabled=True)
    result = CrawlRun(status=CrawlRunStatus.SUCCEEDED.value)
    monkeypatch.setattr(sync, "run_source_by_slug", lambda *args, **kwargs: result)

    exit_code = cli.run(["sync-once", "--source", "firecrawl_self_hosted"])

    assert exit_code == 0
    assert "1 Quellenlaeufe abgeschlossen." in capsys.readouterr().out
