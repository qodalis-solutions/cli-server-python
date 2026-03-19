"""Status controller — server health and runtime information."""

from __future__ import annotations

import os
import platform
import sys
import time
from datetime import datetime, timezone
from typing import Any

import psutil
from fastapi import APIRouter, Depends


def create_status_router(
    start_time: float,
    event_socket_manager: Any,
    auth_dependency: Any,
    enabled_features: list[str] | None = None,
    registry: Any = None,
) -> APIRouter:
    """Create a router that exposes ``GET /status``."""
    router = APIRouter()

    @router.get("/status")
    async def get_status(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> dict[str, Any]:
        uptime_seconds = time.time() - start_time

        proc = psutil.Process(os.getpid())
        mem_info = proc.memory_info()
        memory_usage_mb = mem_info.rss / (1024 * 1024)

        started_at = datetime.fromtimestamp(
            start_time, tz=timezone.utc
        ).isoformat()

        registered_commands = len(registry.processors) if registry is not None else 0

        return {
            "uptimeSeconds": uptime_seconds,
            "memoryUsageMb": round(memory_usage_mb, 2),
            "startedAt": started_at,
            "platform": "python",
            "platformVersion": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
            "activeWsConnections": len(event_socket_manager.get_clients()),
            "activeShellSessions": 0,
            "registeredCommands": registered_commands,
            "registeredJobs": 0,
            "enabledFeatures": enabled_features or [],
        }

    return router
