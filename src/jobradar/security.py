"""Request guards for the private deployment."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .config import Settings, get_settings
from .i18n import locale_from_request, translate


def validate_production_secret(settings: Settings | None = None) -> None:
    runtime = settings or get_settings()
    environment = str(getattr(runtime, "environment", os.getenv("APP_ENV", "development")))
    secret = str(
        getattr(runtime, "secret_key", os.getenv("APP_SECRET_KEY", "development-only-secret"))
    )
    if environment.casefold() not in {"production", "prod"}:
        return
    if "BITTE" in secret or len(secret) < 32:
        raise RuntimeError(
            "Produktionsstart abgebrochen: APP_SECRET_KEY muss sicher ersetzt sein."
        )


def safe_next_path(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-site state-changing browser requests when Origin is present."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in self.SAFE_METHODS:
            return await call_next(request)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.netloc.casefold() != request.url.netloc.casefold():
                if request.url.path.startswith("/api/"):
                    locale = locale_from_request(request)
                    return JSONResponse(
                        {"detail": translate("Ungültiger Anfrageursprung", locale)},
                        status_code=403,
                    )
                locale = locale_from_request(request)
                return Response(translate("Ungültiger Anfrageursprung", locale), status_code=403)
        return await call_next(request)
