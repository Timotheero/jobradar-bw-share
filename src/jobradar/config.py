"""Environment-based application configuration with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

# Local development uses the same documented .env file as Docker Compose.
# Real process variables always win and tests can still change them at runtime.
load_dotenv(override=False)


def _env(name: str, *legacy_names: str, default: str | None = None) -> str | None:
    for candidate in (name, *legacy_names):
        if candidate in os.environ:
            return os.environ[candidate]
    return default


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "ja"}


def _as_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Expected an integer, got {value!r}") from exc


def _as_float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Expected a number, got {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings.

    Network-capable integrations remain disabled unless their explicit
    deployment switch is true.  Secret-bearing fields are hidden from repr so
    accidental diagnostic logging does not reveal them.
    """

    app_name: str = "Jobradar BW"
    environment: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8080
    app_base_url: str = "http://127.0.0.1:8080"
    default_locale: str = "de"
    secret_key: str = field(default="development-only-secret-change-me", repr=False)
    secure_cookies: bool = False
    session_max_age: int = 604_800

    database_url: str = field(default="sqlite:///./jobradar.db", repr=False)
    crawling_enabled: bool = False
    codex_enabled: bool = False
    notifications_enabled: bool = False

    sync_interval_minutes: int = 360
    default_page_size: int = 50
    max_page_size: int = 200
    default_region: str = "Baden-Wuerttemberg"
    default_employment_scope: str = "full_time"
    default_max_commute_minutes: int = 75
    default_commute_devalue_from_minutes: int = 60

    firecrawl_enabled: bool = True
    ba_jobs_enabled: bool = True
    firecrawl_base_url: str = "http://firecrawl-api:3002"
    firecrawl_api_key: str = field(default="", repr=False)
    job_inactive_after_days: int = 30
    raw_html_retention_days: int = 90
    adzuna_app_id: str = field(default="", repr=False)
    adzuna_api_key: str = field(default="", repr=False)
    jooble_api_key: str = field(default="", repr=False)


    codex_command: str = "codex app-server"
    codex_model: str = ""
    codex_max_jobs_per_run: int = 50
    codex_min_remaining_percent: float = 20.0
    codex_data_dir: str = "/data/codex-workspace"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_name=str(_env("APP_NAME", "JOBRADAR_APP_NAME", default="Jobradar BW")),
            environment=str(_env("APP_ENV", "JOBRADAR_ENV", default="development")),
            app_host=str(_env("APP_HOST", default="127.0.0.1")),
            app_port=_as_int(_env("APP_PORT"), default=8080),
            app_base_url=str(_env("APP_BASE_URL", default="http://127.0.0.1:8080")),
            default_locale=str(_env("DEFAULT_LOCALE", default="de")),
            secret_key=str(
                _env(
                    "APP_SECRET_KEY",
                    default="development-only-secret-change-me",
                )
            ),
            secure_cookies=_as_bool(_env("SECURE_COOKIES"), default=False),
            session_max_age=_as_int(_env("SESSION_MAX_AGE"), default=604_800),
            database_url=str(
                _env(
                    "DATABASE_URL",
                    "JOBRADAR_DATABASE_URL",
                    default="sqlite:///./jobradar.db",
                )
            ),
            crawling_enabled=_as_bool(
                _env("CRAWLING_ENABLED", "JOBRADAR_CRAWLING_ENABLED"),
                default=False,
            ),
            codex_enabled=_as_bool(_env("CODEX_ENABLED"), default=False),
            notifications_enabled=_as_bool(_env("NOTIFICATIONS_ENABLED"), default=False),
            sync_interval_minutes=_as_int(
                _env("SYNC_INTERVAL_MINUTES", "JOBRADAR_SYNC_INTERVAL_MINUTES"),
                default=360,
            ),
            default_page_size=_as_int(
                _env("DEFAULT_PAGE_SIZE", "JOBRADAR_DEFAULT_PAGE_SIZE"), default=50
            ),
            max_page_size=_as_int(_env("MAX_PAGE_SIZE", "JOBRADAR_MAX_PAGE_SIZE"), default=200),
            default_region=str(_env("DEFAULT_REGION", default="Baden-Wuerttemberg")),
            default_employment_scope=str(_env("DEFAULT_EMPLOYMENT_SCOPE", default="full_time")),
            default_max_commute_minutes=_as_int(_env("DEFAULT_MAX_COMMUTE_MINUTES"), default=75),
            default_commute_devalue_from_minutes=_as_int(
                _env("DEFAULT_COMMUTE_DEVALUE_FROM_MINUTES"), default=60
            ),
            firecrawl_enabled=_as_bool(_env("FIRECRAWL_ENABLED"), default=True),
            ba_jobs_enabled=_as_bool(_env("BA_JOBS_ENABLED"), default=True),
            firecrawl_base_url=str(_env("FIRECRAWL_BASE_URL", default="http://firecrawl-api:3002")),
            firecrawl_api_key=str(_env("FIRECRAWL_API_KEY", default="")),
            adzuna_app_id=str(_env("ADZUNA_APP_ID", default="")),
            adzuna_api_key=str(_env("ADZUNA_API_KEY", default="")),
            jooble_api_key=str(_env("JOOBLE_API_KEY", default="")),

            job_inactive_after_days=_as_int(
                _env("JOB_INACTIVE_AFTER_DAYS"), default=30
            ),
            raw_html_retention_days=_as_int(
                _env("RAW_HTML_RETENTION_DAYS"), default=90
            ),
            codex_command=str(_env("CODEX_COMMAND", default="codex app-server")),
            codex_model=str(_env("CODEX_MODEL", default="")),
            codex_max_jobs_per_run=_as_int(_env("CODEX_MAX_JOBS_PER_RUN"), default=50),
            codex_min_remaining_percent=_as_float(
                _env("CODEX_MIN_REMAINING_PERCENT"), default=20.0
            ),
            codex_data_dir=str(_env("CODEX_DATA_DIR", default="/data/codex-workspace")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings_cache() -> None:
    """Clear cached settings, primarily for tests and management commands."""

    get_settings.cache_clear()
