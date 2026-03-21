"""FastAPI router for the Data Explorer API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .data_explorer_executor import DataExplorerExecutor
from .data_explorer_registry import DataExplorerRegistry


class DataExplorerExecuteRequest(BaseModel):
    source: str
    query: str
    parameters: dict[str, Any] | None = None


def create_data_explorer_router(
    registry: DataExplorerRegistry,
    executor: DataExplorerExecutor,
) -> APIRouter:
    """Create and return a FastAPI router with data-explorer endpoints."""
    router = APIRouter()

    @router.get("/sources")
    async def get_sources() -> list[dict[str, Any]]:
        return registry.get_sources()

    @router.post("/execute")
    async def execute(request: DataExplorerExecuteRequest) -> dict[str, Any]:
        from dataclasses import asdict

        result = await executor.execute_async(request.model_dump())
        return asdict(result)

    return router
