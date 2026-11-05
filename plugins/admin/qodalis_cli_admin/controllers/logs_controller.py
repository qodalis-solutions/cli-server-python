"""Logs controller — query the log ring buffer."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services.log_ring_buffer import HISTOGRAM_LEVELS, LogRingBuffer, histogram_level
from ..services.time_range import RangeError, parse_buckets, resolve_range


def _levels(level: str | None) -> list[str]:
    """The histogram levels named in ``level`` (comma-separated);
    :class:`RangeError` for an unknown one."""
    levels: list[str] = []
    for name in (level or "").split(","):
        if not name.strip():
            continue
        normalized = histogram_level(name)
        if normalized is None:
            raise RangeError(f"Unknown level '{name.strip()}': use {', '.join(HISTOGRAM_LEVELS)}.")
        if normalized not in levels:
            levels.append(normalized)
    return levels


def create_logs_router(
    log_buffer: LogRingBuffer,
    auth_dependency: Any,
) -> APIRouter:
    """Create a router with ``GET /logs`` and ``GET /logs/histogram``."""
    router = APIRouter()

    @router.get("/logs")
    async def get_logs(
        _user: dict[str, Any] = Depends(auth_dependency),
        level: str | None = Query(None),
        search: str | None = Query(None),
        service: str | None = Query(None),
        limit: int = Query(100, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        items, total = log_buffer.query(
            level=level,
            search=search,
            service=service.strip() if service and service.strip() else None,
            limit=limit,
            offset=offset,
        )
        return {
            "entries": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @router.get("/logs/histogram")
    async def get_logs_histogram(
        _user: dict[str, Any] = Depends(auth_dependency),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        buckets: str | None = Query(None),
        level: str | None = Query(None),
        search: str | None = Query(None),
        service: str | None = Query(None),
    ) -> Any:
        """Log entries per level in equal time columns of ``[from, to)``
        (default: the last 24 hours), with the filters of ``GET /logs``."""
        try:
            since, until = resolve_range(from_, to)
            count = parse_buckets(buckets)
            levels = _levels(level)
        except RangeError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return log_buffer.histogram(
            since,
            until,
            count,
            levels=levels or None,
            search=search or None,
            service=service.strip() if service and service.strip() else None,
        )

    return router
