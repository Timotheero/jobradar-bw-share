"""Maintenance and server commands."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence

import uvicorn

from .config import get_settings
from .db import get_session_factory
from .i18n import normalize_locale, translate, translate_text
from .migrations import upgrade_database
from .seed import seed_demo


def init_db() -> None:
    """CLI migration hook kept small so offline tests can replace it."""

    upgrade_database(database_url=get_settings().database_url)


def parser() -> argparse.ArgumentParser:
    locale = normalize_locale(get_settings().default_locale)
    result = argparse.ArgumentParser(
        prog="jobradar", description=translate("Jobradar BW verwalten", locale)
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help=translate("Datenbanktabellen anlegen", locale))
    commands.add_parser("seed-demo", help=translate("Lokale Beispieldaten anlegen", locale))
    commands.add_parser("status", help=translate("Sicheren Laufzeitstatus ausgeben", locale))
    serve = commands.add_parser("serve", help=translate("Webanwendung starten", locale))
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    sync = commands.add_parser(
        "sync-once", help=translate("Einmalige Synchronisierung anfordern", locale)
    )
    sync.add_argument("--source", default=None)
    return result


def run(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = get_settings()
    locale = normalize_locale(settings.default_locale)
    if args.command == "init-db":
        init_db()
        try:
            from .services.sync import ensure_default_sources

            with get_session_factory()() as session:
                ensure_default_sources(session)
        except ImportError:
            pass
        print(translate("Datenbank ist bereit.", locale))
        return 0
    if args.command == "seed-demo":
        init_db()
        with get_session_factory()() as session:
            count = seed_demo(session)
        print(
            translate(
                "{count} Beispieldatensaetze wurden angelegt oder aktualisiert.",
                locale,
                count=count,
            )
        )
        return 0
    if args.command == "status":
        print(
            json.dumps(
                {
                    "environment": getattr(settings, "environment", "development"),
                    "crawling_enabled": bool(settings.crawling_enabled),
                    "codex_enabled": bool(getattr(settings, "codex_enabled", False)),
                    "database_configured": bool(settings.database_url),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "serve":
        host = args.host or os.getenv("APP_HOST", "127.0.0.1")
        port = args.port or int(os.getenv("APP_PORT", "8080"))
        uvicorn.run("jobradar.main:app", host=host, port=port, reload=False)
        return 0
    if args.command == "sync-once":
        from .models import CrawlRunStatus
        from .services.sync import SourceNotFoundError, run_all_enabled, run_source_by_slug

        init_db()
        try:
            with get_session_factory()() as session:
                if args.source:
                    run_result = run_source_by_slug(session, args.source, settings=settings)
                    runs = (run_result,)
                    blocked = run_result.status == CrawlRunStatus.BLOCKED.value
                    lock_reason = run_result.lock_reason
                else:
                    batch = run_all_enabled(session, settings=settings)
                    runs = batch.runs
                    blocked = batch.blocked
                    lock_reason = batch.lock_reason
        except SourceNotFoundError as exc:
            print(str(exc))
            return 2
        if blocked:
            print(
                translate_text(lock_reason, locale)
                or translate("Synchronisierung ist gesperrt.", locale)
            )
            return 2
        failed = sum(run.status == CrawlRunStatus.FAILED.value for run in runs)
        print(
            translate(
                "{count} Quellenlaeufe abgeschlossen.", locale, count=len(runs)
            )
        )
        return 1 if failed else 0
    return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
