"""Synchronous SQLAlchemy database setup.

PostgreSQL is the production target.  SQLite remains fully supported for local
development and tests, including foreign-key enforcement.
"""

from __future__ import annotations

from collections.abc import Generator
from threading import RLock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_database_lock = RLock()


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine with safe defaults for either PostgreSQL or SQLite."""

    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool

    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    with _database_lock:
        if _engine is None:
            _engine = build_engine(get_settings().database_url)
        return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    with _database_lock:
        if _session_factory is None:
            _session_factory = sessionmaker(
                bind=get_engine(), expire_on_commit=False, class_=Session
            )
        return _session_factory


def configure_database(database_url: str) -> Engine:
    """Replace the process-local engine.

    This is intentionally explicit and is useful for tests or single-process
    maintenance commands.  Production configuration should use the environment.
    """

    global _engine, _session_factory
    with _database_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = build_engine(database_url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
        return _engine


def get_db() -> Generator[Session, None, None]:
    """FastAPI-compatible request-scoped database dependency."""

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Create all known tables without running external work."""

    # Importing registers model mappers on Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine or get_engine())
