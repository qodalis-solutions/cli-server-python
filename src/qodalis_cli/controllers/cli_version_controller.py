from __future__ import annotations

from typing import Any

from fastapi import APIRouter


def create_cli_version_router() -> APIRouter:
    router = APIRouter()

    @router.get("/versions")
    async def get_versions() -> dict[str, Any]:
        return {
            "supportedVersions": [1, 2],
            "preferredVersion": 2,
            "serverVersion": "2.0.0",
        }

    return router
