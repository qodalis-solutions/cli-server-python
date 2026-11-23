"""Plugins controller — list and toggle registered modules."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..services.module_registry import ModuleRegistry


def create_plugins_router(
    module_registry: ModuleRegistry,
    auth_dependency: Any,
    builder: Any = None,
) -> APIRouter:
    """Create a router with ``GET /plugins``, ``GET /plugins/installed``,
    ``GET /plugins/{id}`` and ``POST /plugins/{id}/toggle``."""
    router = APIRouter()

    @router.get("/plugins/installed")
    async def list_installed_plugins(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> list[dict[str, Any]]:
        """The installed server plugin packages (core, users, admin, jobs,
        storage providers, ...) with their versions, in the order added."""
        plugins = getattr(builder, "installed_plugins", None) or []
        return [p.to_dict() for p in plugins]

    @router.get("/dashboard/extensions")
    async def list_dashboard_extensions(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> JSONResponse:
        """Pages plugins add to the dashboard, in registration order.  Every
        page is listed; the dashboard hides those the user may not see and
        each plugin's own API enforces access."""
        extensions = getattr(builder, "dashboard_extensions", None) or []
        return JSONResponse(
            content={"extensions": [e.to_dict() for e in extensions]},
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/plugins")
    async def list_plugins(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> list[dict[str, Any]]:
        return module_registry.list()

    @router.get("/plugins/{plugin_id}")
    async def get_plugin(
        plugin_id: str,
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> Any:
        try:
            return module_registry.get(plugin_id)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "Plugin not found", "code": "PLUGIN_NOT_FOUND"},
            )

    @router.post("/plugins/{plugin_id}/toggle")
    async def toggle_plugin(
        plugin_id: str,
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> Any:
        try:
            return module_registry.toggle(plugin_id)
        except (KeyError, ValueError):
            return JSONResponse(
                status_code=404,
                content={"error": "Plugin not found", "code": "PLUGIN_NOT_FOUND"},
            )

    return router
