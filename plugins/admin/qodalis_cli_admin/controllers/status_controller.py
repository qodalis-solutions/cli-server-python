"""Status controller — server health and runtime information."""

from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any

from fastapi import APIRouter, Depends

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def create_status_router(
    start_time: float,
    event_socket_manager: Any,
    auth_dependency: Any,
) -> APIRouter:
    """Create a router that exposes ``GET /status``."""
    router = APIRouter()

    @router.get("/status")
    async def get_status(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> dict[str, Any]:
        uptime_seconds = time.time() - start_time

        memory: dict[str, Any] = {}
        if _HAS_PSUTIL:
            proc = psutil.Process(os.getpid())
            mem_info = proc.memory_info()
            memory = {
                "rss": mem_info.rss,
                "heapUsed": mem_info.rss,
                "heapTotal": mem_info.vms,
            }

        return {
            "uptime": uptime_seconds,
            "memory": memory,
            "platform": "python",
            "platformVersion": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
            "pid": os.getpid(),
            "connections": {
                "eventClients": len(event_socket_manager.get_clients()),
            },
        }

    return router
