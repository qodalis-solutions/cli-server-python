"""Plugins controller — list and toggle registered modules."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..services.module_registry import ModuleRegistry


def create_plugins_router(
    module_registry: ModuleRegistry,
    auth_dependency: Any,
) -> APIRouter:
    """Create a router with ``GET /plugins`` and ``POST /plugins/{id}/toggle``."""
    router = APIRouter()

    @router.get("/plugins")
    async def list_plugins(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> list[dict[str, Any]]:
        return module_registry.list()

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
