"""Management routes for the optional Codex connection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...i18n import locale_from_request, translate, translate_text
from .client import CodexProtocolError, CodexUnavailableError
from .provider import CodexAnalysisProvider

router = APIRouter(prefix="/codex", tags=["codex"])


def _provider(request: Request) -> CodexAnalysisProvider:
    provider = getattr(request.app.state, "codex_provider", None)
    if not isinstance(provider, CodexAnalysisProvider):
        locale = locale_from_request(request)
        raise HTTPException(
            status_code=503,
            detail=translate("Codex-Adapter ist nicht eingerichtet.", locale),
        )
    return provider


@router.get("/status")
async def codex_status(request: Request) -> dict[str, Any]:
    provider = _provider(request)
    try:
        return await provider.status()
    except (CodexUnavailableError, CodexProtocolError) as exc:
        raise HTTPException(
            status_code=503,
            detail=translate_text(str(exc), locale_from_request(request)),
        ) from exc


@router.post("/login/device")
async def device_login(request: Request) -> dict[str, Any]:
    provider = _provider(request)
    try:
        result = await provider.begin_login()
    except (CodexUnavailableError, CodexProtocolError) as exc:
        raise HTTPException(
            status_code=409,
            detail=translate_text(str(exc), locale_from_request(request)),
        ) from exc
    return {
        "login_id": result.get("loginId"),
        "verification_url": result.get("verificationUrl"),
        "user_code": result.get("userCode"),
    }


@router.post("/logout", status_code=204)
async def codex_logout(request: Request) -> Response:
    provider = _provider(request)
    try:
        await provider.logout()
    except (CodexUnavailableError, CodexProtocolError) as exc:
        raise HTTPException(
            status_code=409,
            detail=translate_text(str(exc), locale_from_request(request)),
        ) from exc
    return Response(status_code=204)
