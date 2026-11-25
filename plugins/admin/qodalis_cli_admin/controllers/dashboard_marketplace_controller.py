"""Dashboard plugins from the marketplace (``/api/v1/qcli/dashboard/marketplace``): reading needs ``admin:access``,
installing, updating and removing ``dashboard:install``.

- ``GET /`` — ``{ marketplaceUrl, install, dashboardApi }``
- ``GET /plugins?query=&page=&pageSize=`` — the marketplace's dashboard plugins, annotated (``installed``,
  ``updateAvailable``, ``allowed``, ``blockedReason``, ``requires``)
- ``GET /plugins/{name}`` — one plugin with its versions
- ``GET /installed`` — the installed plugins (``updateAvailable`` when the marketplace answers)
- ``GET /installed/{name}/config`` — ``{ values, version }``: its settings' values (stored over defaults), the
  section's version
- ``POST /installed`` ``{ name, version?, config? }`` — 201 with the installed plugin (``warnings`` when its settings
  were ignored)
- ``PUT /installed/{name}`` ``{ version?, config? }`` — the updated plugin with ``configDropped``
- ``DELETE /installed/{name}`` — the removed plugin (its settings deleted)

``config`` (``{ key: value }``) is checked against the version's settings before anything is downloaded: 400
``{ error: 'validation failed', errors: { key: message } }``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions.config import has_cli_permission

from ..services.dashboard_marketplace import (
    DASHBOARD_INSTALL_PERMISSION,
    DashboardMarketplace,
    DashboardMarketplaceActor,
    DashboardMarketplaceError,
)
from .config_controller import _gate_context

logger = logging.getLogger("qcli-admin")

_NO_STORE = {"Cache-Control": "no-store"}


def _caller(request: Request, user: Any) -> tuple[DashboardMarketplaceActor, list[str]]:
    """Who is asking and what they may do: the server's caller on a protected server, else the dashboard session
    (a users session carries its permissions; the static admin of password sign-in may do everything)."""
    gate = _gate_context(request)
    if gate is not None and getattr(gate, "user", None) is not None:
        return (
            DashboardMarketplaceActor(user_name=str(getattr(gate.user, "name", "") or ""), user_id=getattr(gate.user, "id", None)),
            list(getattr(gate, "permissions", None) or []),
        )
    session = user if isinstance(user, dict) else {}
    permissions = session.get("permissions")
    return (
        DashboardMarketplaceActor(user_name=session.get("username") or "admin", user_id=session.get("userId")),
        list(permissions) if isinstance(permissions, (list, tuple)) else ["*"],
    )


def _json(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=_NO_STORE)


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DashboardMarketplaceError):
        body: dict[str, Any] = {"error": exc.message}
        if exc.errors:
            body["errors"] = exc.errors
        return _json(body, exc.status)
    logger.error("dashboard marketplace request failed: %s", exc)
    return _json({"error": "internal server error"}, 500)


def _int_param(value: Optional[str], name: str, maximum: int) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not value.strip().lstrip("-").isdigit() or n < 1 or n > maximum:
        raise DashboardMarketplaceError(400, f"invalid {name}")
    return n


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - no or broken body: no fields
        return {}
    return body if isinstance(body, dict) else {}


def create_dashboard_marketplace_router(marketplace: DashboardMarketplace, auth_dependency: Any) -> APIRouter:
    """The marketplace routes, to be included at ``/dashboard/marketplace`` below the API prefix."""
    router = APIRouter()

    def can_install(request: Request, user: Any) -> DashboardMarketplaceActor | JSONResponse:
        actor, permissions = _caller(request, user)
        if not has_cli_permission(permissions, DASHBOARD_INSTALL_PERMISSION):
            return _json({"error": f"forbidden: missing permission {DASHBOARD_INSTALL_PERMISSION}"}, 403)
        return actor

    @router.get("")
    @router.get("/")
    async def info(_user: Any = Depends(auth_dependency)) -> JSONResponse:
        return _json(marketplace.info())

    @router.get("/plugins")
    async def search(request: Request, _user: Any = Depends(auth_dependency)) -> JSONResponse:
        try:
            q = request.query_params
            return _json(await marketplace.search(
                q.get("query"), _int_param(q.get("page"), "page", 1_000_000), _int_param(q.get("pageSize"), "pageSize", 100),
            ))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.get("/plugins/{name:path}")
    async def details(name: str, _user: Any = Depends(auth_dependency)) -> JSONResponse:
        try:
            return _json(await marketplace.details(name))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.get("/installed")
    async def installed(_user: Any = Depends(auth_dependency)) -> JSONResponse:
        try:
            return _json(await marketplace.installed(check_updates=True))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.get("/installed/{name:path}/config")
    async def plugin_config(name: str, _user: Any = Depends(auth_dependency)) -> JSONResponse:
        try:
            return _json(await marketplace.plugin_config(name))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.post("/installed")
    async def install(request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        actor = can_install(request, user)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await _body(request)
            return _json(
                await marketplace.install_plugin(body.get("name"), body.get("version"), actor, body.get("config")), 201,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.put("/installed/{name:path}")
    async def update(name: str, request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        actor = can_install(request, user)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await _body(request)
            result = await marketplace.update_plugin(name, body.get("version"), actor, body.get("config"))
            out = {**result["plugin"], "configDropped": result["configDropped"]}
            if result.get("warnings"):
                out["warnings"] = result["warnings"]
            return _json(out)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.delete("/installed/{name:path}")
    async def remove(name: str, request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        actor = can_install(request, user)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            return _json(await marketplace.remove_plugin(name, actor))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    return router
