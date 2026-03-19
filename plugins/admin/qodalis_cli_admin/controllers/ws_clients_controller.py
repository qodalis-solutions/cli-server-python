"""WebSocket clients controller — list connected WS clients."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends


def create_ws_clients_router(
    event_socket_manager: Any,
    auth_dependency: Any,
) -> APIRouter:
    """Create a router with ``GET /ws/clients``."""
    router = APIRouter()

    @router.get("/ws/clients")
    async def get_ws_clients(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> list[dict[str, Any]]:
        return event_socket_manager.get_clients()

    return router
