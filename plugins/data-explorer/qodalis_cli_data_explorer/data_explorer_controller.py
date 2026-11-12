"""FastAPI router for the Data Explorer API."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .data_explorer_executor import DataExplorerExecutor, result_to_dict
from .data_explorer_policy import filter_schema, missing_permissions
from .data_explorer_registry import DataExplorerRegistry

#: Who makes a request (a ``DataExplorerCaller``: permissions, rate limit,
#: audit); the server supplies it.
CallerResolver = Callable[[Request], Any]


def _to_camel(snake: str) -> str:
    """Convert a snake_case string to camelCase."""
    parts = snake.split("_")
    return parts[0] + "".join(w.capitalize() for w in parts[1:])


def _camel_keys(obj: Any, *, max_depth: int = 2, _depth: int = 0) -> Any:
    """Convert dict keys from snake_case to camelCase down to ``max_depth``
    levels of objects, preserving row data as-is."""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            new_key = _to_camel(k) if _depth < max_depth else k
            # Don't recurse into "rows" — that's user data
            if new_key == "rows":
                result[new_key] = v
            else:
                result[new_key] = _camel_keys(v, max_depth=max_depth, _depth=_depth + 1)
        return result
    if isinstance(obj, list):
        return [_camel_keys(item, max_depth=max_depth, _depth=_depth) for item in obj]
    return obj


class DataExplorerExecuteRequest(BaseModel):
    """Request body for executing a data explorer query."""

    source: str
    query: str
    parameters: dict[str, Any] | None = None


def create_data_explorer_router(
    registry: DataExplorerRegistry,
    executor: DataExplorerExecutor,
    *,
    caller: Optional[CallerResolver] = None,
) -> APIRouter:
    """Create and return a FastAPI router with data-explorer endpoints.

    - ``GET /sources``: the data sources the caller may use
    - ``GET /schema``: a source's schema (allowed tables, without dropped columns)
    - ``POST /execute``: run a query (403 without the source's permissions,
      429 over its rate limit)

    ``caller`` tells who makes a request (the server supplies it)."""
    router = APIRouter()

    def caller_of(request: Request) -> Any:
        return caller(request) if caller is not None else None

    @router.get("/sources")
    async def get_sources(http_request: Request) -> list[dict[str, Any]]:
        return _camel_keys(registry.get_sources(caller_of(http_request)))

    @router.post("/execute")
    async def execute(request: DataExplorerExecuteRequest, http_request: Request) -> Any:
        result = await executor.execute_async(request.model_dump(), caller_of(http_request))
        code = getattr(result, "error_code", None)
        if code == "forbidden":
            return JSONResponse(status_code=403, content={"error": result.error})
        if code == "rate_limited":
            return JSONResponse(status_code=429, content={"error": result.error})
        return _camel_keys(result_to_dict(result))

    @router.get("/schema")
    async def get_schema(source: str, http_request: Request) -> Any:
        entry = registry.get(source)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown data source: '{source}'")

        provider, options = entry
        missing = missing_permissions(getattr(options, "required_permissions", None), caller_of(http_request))
        if missing:
            return JSONResponse(status_code=403, content={"error": f"forbidden: missing permission {', '.join(missing)}"})
        schema = await provider.get_schema_async(options)
        if schema is None:
            raise HTTPException(status_code=404, detail="Schema introspection is not supported by this data source.")

        # Three levels: schema → tables → columns (``primaryKey``)
        return _camel_keys(asdict(filter_schema(schema, options)), max_depth=3)

    return router
