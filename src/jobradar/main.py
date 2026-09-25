"""FastAPI application factory for Jobradar BW."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from .api import router as core_api_router
from .config import Settings, get_settings
from .db import get_session_factory
from .integrations.codex.client import CodexAppServerClient
from .integrations.codex.provider import CodexAnalysisProvider
from .integrations.codex.routes import router as codex_router
from .migrations import upgrade_database
from .security import SameOriginMiddleware, validate_production_secret
from .web import router as web_router


def _setting(settings: Settings, name: str, environment_name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    if value is not None:
        return value
    raw = os.getenv(environment_name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.casefold() in {"1", "true", "yes", "on", "ja"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        validate_production_secret(runtime)
        upgrade_database(database_url=runtime.database_url)
        try:
            from .services.sync import ensure_default_sources

            with get_session_factory()() as session:
                ensure_default_sources(session)
        except ImportError:
            pass
        yield
        client = getattr(application.state, "codex_client", None)
        if isinstance(client, CodexAppServerClient) and client.running:
            await client.close()

    application = FastAPI(
        title=str(_setting(runtime, "app_name", "APP_NAME", "Jobradar BW")),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    application.state.settings = runtime

    codex_client = CodexAppServerClient(
        str(_setting(runtime, "codex_command", "CODEX_COMMAND", "codex app-server"))
    )
    application.state.codex_client = codex_client
    application.state.codex_provider = CodexAnalysisProvider(
        codex_client,
        enabled=bool(_setting(runtime, "codex_enabled", "CODEX_ENABLED", False)),
        workspace=str(
            _setting(runtime, "codex_data_dir", "CODEX_DATA_DIR", "/data/codex-workspace")
        ),
        model=str(_setting(runtime, "codex_model", "CODEX_MODEL", "")),
        max_jobs_per_batch=int(
            _setting(runtime, "codex_max_jobs_per_run", "CODEX_MAX_JOBS_PER_RUN", 50)
        ),
        minimum_remaining_percent=float(
            _setting(
                runtime,
                "codex_min_remaining_percent",
                "CODEX_MIN_REMAINING_PERCENT",
                20.0,
            )
        ),
    )

    application.add_middleware(SameOriginMiddleware)
    application.add_middleware(
        SessionMiddleware,
        secret_key=str(
            _setting(
                runtime,
                "secret_key",
                "APP_SECRET_KEY",
                "development-only-secret-change-me",
            )
        ),
        session_cookie="jobradar_session",
        max_age=int(_setting(runtime, "session_max_age", "SESSION_MAX_AGE", 604800)),
        same_site="lax",
        https_only=bool(_setting(runtime, "secure_cookies", "SECURE_COOKIES", False)),
    )

    @application.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with get_session_factory()() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok"})

    application.include_router(core_api_router, prefix="/api")
    application.include_router(codex_router, prefix="/api")
    application.include_router(web_router)
    return application


app = create_app()
