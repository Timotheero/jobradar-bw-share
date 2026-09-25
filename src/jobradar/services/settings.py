"""Application settings for API retrieval and separately permitted web crawling."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings as get_runtime_settings
from ..models import AppSetting

CRAWLING_SETTING_KEY = "system.crawling_enabled"
FIRECRAWL_SETTING_KEY = "system.firecrawl_enabled"
CRAWLING_DESCRIPTION = (
    "Application switch for scheduled and manual API/feed retrieval. "
    "The deployment environment must also allow external source access."
)
FIRECRAWL_DESCRIPTION = (
    "Separate application permission for fetching configured websites through Firecrawl."
)


class CrawlingLockedError(RuntimeError):
    pass


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(AppSetting, key)
    return default if row is None else row.value_json


def set_setting(
    session: Session,
    key: str,
    value: Any,
    *,
    description: str | None = None,
    commit: bool = True,
) -> AppSetting:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json=value, description=description)
        session.add(row)
    else:
        row.value_json = value
        if description is not None:
            row.description = description
    if commit:
        session.commit()
        session.refresh(row)
    else:
        session.flush()
    return row


def list_settings(session: Session) -> list[AppSetting]:
    return list(session.scalars(select(AppSetting).order_by(AppSetting.key)))


def database_crawling_switch(session: Session) -> bool:
    """Return the application switch for scheduled and manual API/feed retrieval."""

    return bool(get_setting(session, CRAWLING_SETTING_KEY, False))


def database_firecrawl_switch(session: Session) -> bool:
    """Return the explicit website-crawling permission, disabled by default."""

    return bool(get_setting(session, FIRECRAWL_SETTING_KEY, False))


def is_crawling_enabled(session: Session) -> bool:
    """Return whether the deployment and application allow API/feed retrieval."""

    return bool(get_runtime_settings().crawling_enabled and database_crawling_switch(session))


def is_firecrawl_enabled(session: Session) -> bool:
    """Return whether Firecrawl has its additional deployment and application permission."""

    runtime = get_runtime_settings()
    return bool(
        runtime.crawling_enabled
        and runtime.firecrawl_enabled
        and database_crawling_switch(session)
        and database_firecrawl_switch(session)
    )


def set_crawling_enabled(session: Session, enabled: bool) -> AppSetting:
    """Change the API/feed switch without bypassing the deployment-level lock."""

    if enabled and not get_runtime_settings().crawling_enabled:
        raise CrawlingLockedError(
            "API- und Feed-Abrufe bleiben gesperrt: CRAWLING_ENABLED ist auf dem Server "
            "nicht aktiviert."
        )
    return set_setting(
        session,
        CRAWLING_SETTING_KEY,
        bool(enabled),
        description=CRAWLING_DESCRIPTION,
    )


def set_firecrawl_enabled(session: Session, enabled: bool) -> AppSetting:
    """Change the separate Firecrawl permission without bypassing deployment locks."""

    runtime = get_runtime_settings()
    if enabled and not runtime.crawling_enabled:
        raise CrawlingLockedError(
            "Firecrawl bleibt gesperrt: CRAWLING_ENABLED ist auf dem Server "
            "nicht aktiviert."
        )
    if enabled and not runtime.firecrawl_enabled:
        raise CrawlingLockedError(
            "Firecrawl bleibt gesperrt: FIRECRAWL_ENABLED ist auf dem Server "
            "nicht aktiviert."
        )
    return set_setting(
        session,
        FIRECRAWL_SETTING_KEY,
        bool(enabled),
        description=FIRECRAWL_DESCRIPTION,
    )


def assert_crawling_allowed(session: Session) -> None:
    if not is_crawling_enabled(session):
        raise CrawlingLockedError(
            "API- und Feed-Abrufe sind gesperrt. Serverfreigabe und "
            "Anwendungsschalter müssen aktiv sein."
        )
