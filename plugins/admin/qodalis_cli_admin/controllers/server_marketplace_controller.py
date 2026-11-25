"""Server plugins from the marketplace (``/api/v1/qcli/server/marketplace``): reading needs ``admin:access``,
installing, updating and removing ``plugins:install``.  Every change takes effect after a restart: 202 with
``restartRequired``.

- ``GET /`` — ``{ marketplaceUrl, install, platform: 'python', serverVersion, enabled }``
- ``GET /plugins?query=&page=&pageSize=`` — the marketplace's server plugins with a Python package, annotated
  (``platform``, ``otherPlatforms``, ``autoInstall``, ``needsConfiguration``, ``updateAvailable``, ``allowed``,
  ``blockedReason``)
- ``GET /plugins/{name}`` — one plugin with its platforms and versions
- ``GET /installed`` — the installed plugins and how they run (``status``, ``loadedVersion``, ``error``, ``missing``,
  ``updateAvailable``)
- ``POST /installed`` ``{ name, version? }`` — 202, the installed plugin with ``restartRequired``
- ``PUT /installed/{name}`` ``{ version? }`` — 202 (200 with ``changed: false`` when it already is that version)
- ``DELETE /installed/{name}`` — 202, the removed plugin
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions.config import has_cli_permission

from ..services.dashboard_marketplace import DashboardMarketplaceActor
from ..services.server_plugin_marketplace import PLUGINS_INSTALL_PERMISSION, ServerPluginMarketplace
from .dashboard_marketplace_controller import _body, _caller, _error, _int_param, _json


def create_server_marketplace_router(marketplace: ServerPluginMarketplace, auth_dependency: Any) -> APIRouter:
    """The server marketplace routes, to be included at ``/server/marketplace`` below the API prefix."""
    router = APIRouter()

    def can_install(request: Request, user: Any) -> DashboardMarketplaceActor | JSONResponse:
        actor, permissions = _caller(request, user)
        if not has_cli_permission(permissions, PLUGINS_INSTALL_PERMISSION):
            return _json({"error": f"forbidden: missing permission {PLUGINS_INSTALL_PERMISSION}"}, 403)
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

    @router.post("/installed")
    async def install(request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        actor = can_install(request, user)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await _body(request)
            return _json(await marketplace.install_plugin(body.get("name"), body.get("version"), actor), 202)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.put("/installed/{name:path}")
    async def update(name: str, request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        actor = can_install(request, user)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await _body(request)
            result = await marketplace.update_plugin(name, body.get("version"), actor)
            return _json(result, 200 if result.get("changed") is False else 202)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    @router.delete("/installed/{name:path}")
    async def remove(name: str, request: Request, user: Any = Depends(auth_dependency)) -> JSONResponse:
        actor = can_install(request, user)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            return _json(await marketplace.remove_plugin(name, actor), 202)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    return router
