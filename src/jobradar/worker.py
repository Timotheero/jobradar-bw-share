"""Interval worker for the safely gated synchronization service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Callable, Sequence
from threading import Event

from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .db import get_session_factory
from .integrations.codex.client import CodexAppServerClient
from .integrations.codex.provider import CodexAnalysisProvider
from .services.availability import HTTPJobAvailabilityChecker, JobAvailabilityChecker
from .services.job_summary import JobSummaryProvider, precompute_summaries
from .services.maintenance import run_job_maintenance
from .services.rescore import process_pending_rescore
from .services.sync import (
    ConnectorFactoryLike,
    SyncQueryPlan,
    query_plan_for_preferences,
    run_all_enabled,
    run_source_by_slug,
)

LOGGER = logging.getLogger("jobradar.worker")
DEFAULT_INTERVAL_MINUTES = 360


def interval_minutes_from_env() -> int:
    raw = os.getenv(
        "JOBRADAR_SYNC_INTERVAL_MINUTES",
        os.getenv("SYNC_INTERVAL_MINUTES", str(DEFAULT_INTERVAL_MINUTES)),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("SYNC_INTERVAL_MINUTES muss eine ganze Zahl sein.") from exc
    if value < 1:
        raise ValueError("SYNC_INTERVAL_MINUTES muss mindestens 1 sein.")
    return value


def excluded_source_slugs_from_env() -> tuple[str, ...]:
    raw = os.getenv("JOBRADAR_SYNC_EXCLUDE_SOURCES", "")
    return tuple(dict.fromkeys(slug.strip() for slug in raw.split(",") if slug.strip()))


def run_worker(
    *,
    session_factory: sessionmaker[Session] | Callable[[], Session] | None = None,
    settings: Settings | None = None,
    connector_factory: ConnectorFactoryLike | None = None,
    query_plan: SyncQueryPlan | None = None,
    source_slug: str | None = None,
    exclude_source_slugs: tuple[str, ...] = (),
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    once: bool = False,
    summary_provider: JobSummaryProvider | None = None,
    availability_checker: JobAvailabilityChecker | None = None,
    stop_event: Event | None = None,
) -> None:
    """Run synchronization once or every configured interval.

    ``Event.wait`` makes the six-hour wait immediately interruptible on SIGTERM.
    The synchronization service checks deployment and API/feed permissions before
    constructing connectors, plus the separate Firecrawl permission before that source.
    """

    if interval_minutes < 1:
        raise ValueError("interval_minutes must be at least 1")
    runtime_settings = settings or get_settings()
    make_session = session_factory or get_session_factory()
    stop = stop_event or Event()
    if source_slug is not None:
        _run_source_worker(
            source_slug,
            make_session=make_session,
            settings=runtime_settings,
            connector_factory=connector_factory,
            query_plan=query_plan,
            interval_minutes=interval_minutes,
            once=once,
            stop=stop,
        )
        return

    while not stop.is_set():
        try:
            with make_session() as session:
                rescore_result = process_pending_rescore(session)
                if rescore_result.status != "idle":
                    LOGGER.info(
                        "Lokale Neubewertung: %s, %d Stellen",
                        rescore_result.status,
                        rescore_result.processed_jobs,
                    )
        except Exception:
            LOGGER.exception("Unerwarteter Fehler bei der lokalen Neubewertung")

        try:
            with make_session() as session:
                effective_query_plan = query_plan
                if effective_query_plan is None:
                    from .services.profile import get_preference_profile

                    preference = get_preference_profile(session)
                    if preference is not None:
                        effective_query_plan = query_plan_for_preferences(preference)
                if not runtime_settings.crawling_enabled:
                    LOGGER.info("Externe Quellenzugriffe sind serverseitig gesperrt.")
                result = run_all_enabled(
                    session,
                    settings=runtime_settings,
                    connector_factory=connector_factory,
                    query_plan=effective_query_plan,
                    run_type="scheduled",
                    exclude_source_slugs=exclude_source_slugs,
                )
                if result.blocked:
                    LOGGER.info("Synchronisierung gesperrt: %s", result.lock_reason)
                else:
                    LOGGER.info(
                        "Synchronisierung beendet: %d neu, %d aktualisiert, %d Fehler",
                        result.created_count,
                        result.updated_count,
                        result.error_count,
                    )
        except Exception:
            LOGGER.exception("Unerwarteter Fehler im Synchronisierungszyklus")

        try:
            checker = availability_checker or HTTPJobAvailabilityChecker()
            try:
                with make_session() as session:
                    maintenance = run_job_maintenance(
                        session,
                        runtime_settings,
                        checker,
                    )
            finally:
                if availability_checker is None:
                    checker.close()
            LOGGER.info(
                "Stellenpflege: %d geprüft, %d inaktiv, %d reaktiviert, "
                "%d Prüfungen fehlgeschlagen, %d Rohinhalte bereinigt",
                maintenance.jobs_checked,
                maintenance.jobs_inactivated,
                maintenance.jobs_reactivated,
                maintenance.checks_failed,
                maintenance.raw_snapshots_cleaned,
            )
        except Exception:
            LOGGER.exception("Unerwarteter Fehler bei der Stellenpflege")

        if runtime_settings.codex_enabled:
            try:
                result = asyncio.run(
                    _precompute_summary_cycle(
                        make_session,
                        runtime_settings,
                        provider=summary_provider,
                    )
                )
                LOGGER.info(
                    "Automatische Zusammenfassungen: %d erstellt, %d relevante Stellen",
                    result.generated_jobs,
                    result.eligible_jobs,
                )
            except Exception:
                LOGGER.exception("Unerwarteter Fehler bei automatischen Zusammenfassungen")

        if once:
            return
        stop.wait(interval_minutes * 60)


def _run_source_worker(
    source_slug: str,
    *,
    make_session: sessionmaker[Session] | Callable[[], Session],
    settings: Settings,
    connector_factory: ConnectorFactoryLike | None,
    query_plan: SyncQueryPlan | None,
    interval_minutes: int,
    once: bool,
    stop: Event,
) -> None:
    slug = source_slug.strip()
    if not slug:
        raise ValueError("source_slug must not be empty")

    while not stop.is_set():
        try:
            with make_session() as session:
                effective_query_plan = query_plan
                if effective_query_plan is None:
                    from .services.profile import get_preference_profile

                    preference = get_preference_profile(session)
                    if preference is not None:
                        effective_query_plan = query_plan_for_preferences(preference)
                run = run_source_by_slug(
                    session,
                    slug,
                    settings=settings,
                    connector_factory=connector_factory,
                    query_plan=effective_query_plan,
                    run_type="scheduled",
                )
                if run.status == "blocked":
                    LOGGER.info("Synchronisierung für %s gesperrt: %s", slug, run.lock_reason)
                else:
                    LOGGER.info(
                        "Synchronisierung für %s beendet: %d neu, %d aktualisiert, %d Fehler",
                        slug,
                        run.created_count,
                        run.updated_count,
                        run.error_count,
                    )
        except Exception:
            LOGGER.exception("Unerwarteter Fehler im Synchronisierungszyklus für %s", slug)

        if once:
            return
        stop.wait(interval_minutes * 60)


async def _precompute_summary_cycle(
    make_session: sessionmaker[Session] | Callable[[], Session],
    settings: Settings,
    *,
    provider: JobSummaryProvider | None,
):
    client: CodexAppServerClient | None = None
    effective_provider = provider
    if effective_provider is None:
        client = CodexAppServerClient(settings.codex_command)
        effective_provider = CodexAnalysisProvider(
            client,
            enabled=True,
            workspace=settings.codex_data_dir,
            model=settings.codex_model,
            max_jobs_per_batch=settings.codex_max_jobs_per_run,
            minimum_remaining_percent=settings.codex_min_remaining_percent,
        )
    try:
        with make_session() as session:
            return await precompute_summaries(
                session,
                effective_provider,
                min_relevance=60,
                limit=max(1, min(100, settings.codex_max_jobs_per_run)),
                locale=settings.default_locale,
            )
    finally:
        if client is not None:
            await client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jobradar-Synchronisierungsworker")
    parser.add_argument("--once", action="store_true", help="Genau einen Zyklus ausfuehren")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=interval_minutes_from_env(),
        help="Intervall zwischen Abrufen; Standard 360 Minuten",
    )
    parser.add_argument(
        "--source",
        default=os.getenv("JOBRADAR_SYNC_SOURCE"),
        help="Nur die angegebene Quelle regelmäßig synchronisieren",
    )
    parser.add_argument(
        "--exclude-source",
        action="append",
        default=list(excluded_source_slugs_from_env()),
        help="Quelle aus dem allgemeinen Synchronisierungszyklus ausschließen",
    )
    return parser


def _install_signal_handlers(stop: Event) -> None:
    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Signal %s empfangen; Worker wird sauber beendet.", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_stop)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = Event()
    _install_signal_handlers(stop)
    run_worker(
        interval_minutes=args.interval_minutes,
        source_slug=args.source,
        exclude_source_slugs=tuple(args.exclude_source),
        once=args.once,
        stop_event=stop,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
