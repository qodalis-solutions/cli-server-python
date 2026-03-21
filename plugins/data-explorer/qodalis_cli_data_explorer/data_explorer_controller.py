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


def _camel_keys(obj: Any, *, _depth: int = 0) -> Any:
    """Convert dict keys from snake_case to camelCase.

    Only converts structural keys (depth 0 = result envelope, depth 1 = source
    info dicts).  Row/document data inside ``rows`` is left untouched so that
    MongoDB fields like ``_id`` are preserved as-is.
    """
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            new_key = _to_camel(k) if _depth < 2 else k
            # Don't recurse into "rows" — that's user data
            if new_key == "rows":
                result[new_key] = v
            else:
                result[new_key] = _camel_keys(v, _depth=_depth + 1)
        return result
    if isinstance(obj, list):
        return [_camel_keys(item, _depth=_depth) for item in obj]
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

    @router.get("/schema")
    async def get_schema(source: str) -> dict[str, Any]:
        entry = registry.get(source)
        if entry is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Unknown data source: '{source}'")

        provider, options = entry
        schema = await provider.get_schema_async(options)
        if schema is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Schema introspection is not supported by this data source.")

        return _camel_keys(asdict(schema))

    return router
