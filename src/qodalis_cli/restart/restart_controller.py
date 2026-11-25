"""``/api/v1/qcli/server/restart``: ``GET`` the status (``admin:access`` on a protected server), ``POST { reason? }``
restarts (``server:restart``): 202 ``{ restarting: true, pending }``, then the graceful shutdown.  The route
permissions are checked by the server's auth gate (the users plugin)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions.restart import (
    CliServerRestartActor,
    CliServerRestartError,
    CliServerRestartRequest,
)

from ..security import get_auth_context
from .cli_server_restart import CliServerRestart

logger = logging.getLogger("qodalis_cli.restart")

_NO_STORE = {"Cache-Control": "no-store"}


def restart_actor(auth: Any) -> CliServerRestartActor | None:
    """The signed-in caller of an auth context (the users plugin's), or ``None`` without one."""
    user = getattr(auth, "user", None)
    name = getattr(auth, "user_name", None) or getattr(user, "name", None)
    if not name:
        return None
    return CliServerRestartActor(user_name=str(name), user_id=getattr(auth, "user_id", None) or getattr(user, "id", None))


def create_restart_router(restart: CliServerRestart) -> APIRouter:
    router = APIRouter()

    @router.get("")
    @router.get("/")
    async def status() -> JSONResponse:
        return JSONResponse(restart.status(), headers=_NO_STORE)

    @router.post("")
    @router.post("/")
    async def post(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - no or broken body: no reason
            body = None
        reason = body.get("reason") if isinstance(body, dict) else None
        try:
            result = await restart.restart(CliServerRestartRequest(
                reason=reason, actor=restart_actor(get_auth_context(request)), via="api",
            ))
        except CliServerRestartError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status, headers=_NO_STORE)
        except Exception as exc:  # noqa: BLE001
            logger.error("Restart request failed: %s", exc)
            return JSONResponse({"error": "internal server error"}, status_code=500, headers=_NO_STORE)
        return JSONResponse(result, status_code=202, headers=_NO_STORE)

    return router
