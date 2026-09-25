"""Database schema migration entry point."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import get_settings


def _alembic_ini() -> Path:
    candidates = (
        Path.cwd() / "alembic.ini",
        Path("/app/alembic.ini"),
        Path(__file__).resolve().parents[2] / "alembic.ini",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("alembic.ini wurde nicht gefunden; Projektpaket ist unvollstaendig.")


def upgrade_database(revision: str = "head", *, database_url: str | None = None) -> None:
    """Upgrade the configured database to a known schema revision."""

    ini = _alembic_ini()
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "migrations"))
    resolved_database_url = database_url or get_settings().database_url
    config.attributes["database_url_override"] = resolved_database_url
    config.set_main_option(
        "sqlalchemy.url",
        resolved_database_url.replace("%", "%%"),
    )
    command.upgrade(config, revision)
