"""The config API over the server's config providers (``builder.config``)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions.config import (
    CliConfigCaller,
    CliConfigContext,
    CliConfigError,
    cli_config_context,
)

logger = logging.getLogger("qcli-admin")

_NO_STORE = {"Cache-Control": "no-store"}


def _gate_context(request: Request) -> Any:
    """The caller as the users plugin's gate authenticated it (a protected server)."""
    try:
        from qodalis_cli.security import get_auth_context
    except ImportError:  # pragma: no cover - the plugin without the server package
        return None
    return get_auth_context(request)


def config_context_of(request: Request, user: Any) -> CliConfigContext:
    """Who is asking: the server's caller on a protected server, else the
    dashboard session (a users session carries its permissions; the static
    admin of password mode may do everything)."""
    gate = _gate_context(request)
    if gate is not None and getattr(gate, "user", None) is not None:
        caller = CliConfigCaller(
            user_id=getattr(gate.user, "id", None),
            user_name=getattr(gate.user, "name", None),
            permissions=tuple(getattr(gate, "permissions", None) or ()),
        )
        return cli_config_context(caller, "api", has_permission=lambda p: bool(gate.can(p)))
    session = user if isinstance(user, dict) else {}
    permissions = session.get("permissions")
    caller = CliConfigCaller(
        user_id=session.get("userId"),
        user_name=session.get("username") or session.get("sub") or "admin",
        permissions=tuple(permissions) if isinstance(permissions, (list, tuple)) else ("*",),
    )
    return cli_config_context(caller, "api")


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, CliConfigError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)
    logger.error("config request failed: %s", exc)
    return JSONResponse({"error": "internal server error"}, status_code=500)


def create_config_router(get_service: Callable[[], Any], auth_dependency: Any) -> APIRouter:
    """``GET /config`` (``{providers: [...]}``, the providers the caller may
    see, ``config:read``), ``GET /config/{provider}`` and ``PATCH
    /config/{provider}/{section}`` (``{changes, version?}``, ``config:write``):
    200 with the section, 400 ``{error, errors}`` per key, 403, 404, 409
    ``{error, version}`` when the version is stale."""
    router = APIRouter()

    def service_or_404() -> Any:
        return get_service()

    def unavailable() -> JSONResponse:
        return JSONResponse({"error": "config providers are not available on this server"}, status_code=404)

    @router.get("/config")
    async def list_config(request: Request, user: Any = Depends(auth_dependency)) -> Any:
        service = service_or_404()
        if service is None:
            return unavailable()
        try:
            providers = await service.list(config_context_of(request, user))
            return JSONResponse({"store": service.describe_store().to_dict(), "providers": providers}, headers=_NO_STORE)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.get("/config/{provider}")
    async def get_config(provider: str, request: Request, user: Any = Depends(auth_dependency)) -> Any:
        service = service_or_404()
        if service is None:
            return unavailable()
        try:
            return JSONResponse(await service.get(provider, config_context_of(request, user)), headers=_NO_STORE)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.patch("/config/{provider}/{section}")
    async def update_config(provider: str, section: str, request: Request, user: Any = Depends(auth_dependency)) -> Any:
        service = service_or_404()
        if service is None:
            return unavailable()
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
        try:
            result = await service.update(
                provider, section, {"changes": body.get("changes"), "version": body.get("version")},
                config_context_of(request, user),
            )
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    return router
