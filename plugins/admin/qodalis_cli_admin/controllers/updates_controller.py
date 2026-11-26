"""``/api/v1/qcli/updates`` (behind the admin sign-in; ``updates:read`` or ``plugins:install`` on a protected server):

- ``GET /`` → the last check's result (``checkedAt``, ``checking``, ``server``, ``dashboardPlugins``,
  ``serverPlugins``, ``codePlugins``, ``count``, ``error?``);
- ``POST /check`` → checks now and answers the new result (502 ``{ error }`` only when the check itself fails).
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions import CLI_PLUGINS_INSTALL_PERMISSION
from qodalis_cli_server_abstractions.config import has_cli_permission

from ..services.update_checker import UPDATES_READ_PERMISSION, UpdateChecker
from .dashboard_marketplace_controller import _caller

_NO_STORE = {"Cache-Control": "no-store"}


def can_read_updates(permissions: Optional[Sequence[str]]) -> bool:
    """True when the caller may see and check the updates: ``updates:read`` or ``plugins:install`` (anyone without
    sign-in: ``None``)."""
    if permissions is None:
        return True
    return has_cli_permission(permissions, UPDATES_READ_PERMISSION) or has_cli_permission(
        permissions, CLI_PLUGINS_INSTALL_PERMISSION,
    )


def create_updates_router(checker: UpdateChecker, auth_dependency: Any) -> APIRouter:
    """The updates routes, to be included at ``/updates`` below the API prefix."""
    router = APIRouter()

    def refused(request: Request, user: Any) -> Optional[JSONResponse]:
        _actor, permissions = _caller(request, user)
        if can_read_updates(permissions):
            return None
        return JSONResponse({"error": f"forbidden: missing permission {UPDATES_READ_PERMISSION}"}, status_code=403)

    @router.get("")
    @router.get("/")
    async def last_result(request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        denied = refused(request, user)
        if denied is not None:
            return denied
        return JSONResponse(checker.result(), headers=_NO_STORE)

    @router.post("/check")
    async def check(request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        denied = refused(request, user)
        if denied is not None:
            return denied
        try:
            return JSONResponse(await checker.check(), headers=_NO_STORE)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc) or "update check failed"}, status_code=502, headers=_NO_STORE)

    return router
