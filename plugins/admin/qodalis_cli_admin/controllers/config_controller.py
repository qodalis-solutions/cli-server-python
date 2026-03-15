"""Configuration controller — read and update admin settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..services.admin_config import AdminConfig


class UpdateConfigRequest(BaseModel):
    settings: dict[str, Any]


def create_config_router(
    admin_config: AdminConfig,
    auth_dependency: Any,
) -> APIRouter:
    """Create a router with ``GET /config`` and ``PUT /config``."""
    router = APIRouter()

    @router.get("/config")
    async def get_config(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> dict[str, Any]:
        return {"sections": admin_config.get_config_sections()}

    @router.put("/config")
    async def update_config(
        body: UpdateConfigRequest,
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> dict[str, Any]:
        admin_config.update_settings(body.settings)
        return {"message": "Configuration updated", "sections": admin_config.get_config_sections()}

    return router
