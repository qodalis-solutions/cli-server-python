"""FastAPI router for the Data Explorer API."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .data_explorer_executor import DataExplorerExecutor
from .data_explorer_registry import DataExplorerRegistry


def _to_camel(snake: str) -> str:
    """Convert a snake_case string to camelCase."""
    parts = snake.split("_")
    return parts[0] + "".join(w.capitalize() for w in parts[1:])


def _camel_keys(obj: Any) -> Any:
    """Recursively convert all dict keys from snake_case to camelCase."""
    if isinstance(obj, dict):
        return {_to_camel(k): _camel_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camel_keys(item) for item in obj]
    return obj


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
        return _camel_keys(registry.get_sources())

    @router.post("/execute")
    async def execute(request: DataExplorerExecuteRequest) -> dict[str, Any]:
        result = await executor.execute_async(request.model_dump())
        return _camel_keys(asdict(result))

    return router
