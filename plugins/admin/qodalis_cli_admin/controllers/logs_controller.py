"""Logs controller — query the log ring buffer."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..services.log_ring_buffer import LogRingBuffer


def create_logs_router(
    log_buffer: LogRingBuffer,
    auth_dependency: Any,
) -> APIRouter:
    """Create a router with ``GET /logs``."""
    router = APIRouter()

    @router.get("/logs")
    async def get_logs(
        _user: dict[str, Any] = Depends(auth_dependency),
        level: str | None = Query(None),
        search: str | None = Query(None),
        limit: int = Query(100, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        items, total = log_buffer.query(
            level=level,
            search=search,
            limit=limit,
            offset=offset,
        )
        return {
            "entries": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    return router
