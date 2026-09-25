from __future__ import annotations

import pytest

from jobradar.config import Settings
from jobradar.security import validate_production_secret


def test_documented_environment_names_drive_runtime(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "Mein Jobradar")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEFAULT_LOCALE", "en")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example.invalid/db")
    monkeypatch.setenv("CRAWLING_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_MIN_REMAINING_PERCENT", "25.5")
    monkeypatch.setenv("RAW_HTML_RETENTION_DAYS", "90")
    monkeypatch.setenv("JOB_INACTIVE_AFTER_DAYS", "45")

    settings = Settings.from_env()

    assert settings.app_name == "Mein Jobradar"
    assert settings.environment == "production"
    assert settings.default_locale == "en"
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.crawling_enabled is True
    assert settings.codex_enabled is True
    assert settings.codex_min_remaining_percent == 25.5
    assert settings.raw_html_retention_days == 90
    assert settings.job_inactive_after_days == 45


def test_primary_names_override_legacy_names(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///primary.db")
    monkeypatch.setenv("JOBRADAR_DATABASE_URL", "sqlite:///legacy.db")
    monkeypatch.setenv("CRAWLING_ENABLED", "false")
    monkeypatch.setenv("JOBRADAR_CRAWLING_ENABLED", "true")

    settings = Settings.from_env()

    assert settings.database_url == "sqlite:///primary.db"
    assert settings.crawling_enabled is False


def test_secret_values_are_not_in_settings_repr() -> None:
    settings = Settings(
        secret_key="top-secret-session-key",
        database_url="postgresql://user:top-secret-db-password@db/app",
        firecrawl_api_key="top-secret-firecrawl-key",
    )

    rendered = repr(settings)

    assert "top-secret" not in rendered


def test_production_requires_only_a_strong_session_secret() -> None:
    with pytest.raises(RuntimeError, match="APP_SECRET_KEY"):
        validate_production_secret(Settings(environment="production", secret_key="too-short"))

    validate_production_secret(
        Settings(environment="production", secret_key="a-secure-session-secret-with-32-bytes")
    )
