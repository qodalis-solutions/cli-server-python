"""Status controller — server health and runtime information."""

from __future__ import annotations

import platform
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services.status_history import StatusHistory, memory_usage_mb

#: ``GET /status/history``: default and longest window in minutes.
DEFAULT_HISTORY_MINUTES = 60
MAX_HISTORY_MINUTES = 1440


def create_status_router(
    start_time: float,
    event_socket_manager: Any,
    auth_dependency: Any,
    enabled_features: list[str] | None = None,
    registry: Any = None,
    builder: Any = None,
    status_history: StatusHistory | None = None,
) -> APIRouter:
    """Create a router that exposes ``GET /status`` and ``GET /status/history``."""
    router = APIRouter()

    @router.get("/status")
    async def get_status(
        _user: dict[str, Any] = Depends(auth_dependency),
    ) -> dict[str, Any]:
        uptime_seconds = time.time() - start_time

        started_at = datetime.fromtimestamp(
            start_time, tz=timezone.utc
        ).isoformat()

        registered_commands = len(registry.processors) if registry is not None else 0

        return {
            "uptimeSeconds": uptime_seconds,
            "memoryUsageMb": memory_usage_mb(),
            "startedAt": started_at,
            "platform": "python",
            "platformVersion": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
            "activeWsConnections": len(event_socket_manager.get_clients()),
            "activeShellSessions": 0,
            "registeredCommands": registered_commands,
            "registeredJobs": 0,
            "enabledFeatures": _features(enabled_features, builder),
        }

    @router.get("/status/history")
    async def get_status_history(
        _user: dict[str, Any] = Depends(auth_dependency),
        minutes: str | None = Query(None),
    ) -> Any:
        """The status samples (every 15 s, kept 24 h) of the last ``minutes``
        (default 60, clamped to 1..1440), oldest first."""
        window = DEFAULT_HISTORY_MINUTES
        if minutes is not None and minutes.strip():
            try:
                window = int(minutes.strip())
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"'minutes' must be an integer, got '{minutes}'"},
                )
        window = min(max(window, 1), MAX_HISTORY_MINUTES)
        history = status_history or StatusHistory(event_socket_manager)
        return {
            "intervalSeconds": history.interval,
            "samples": history.history(window),
        }

    return router


JOBS_DISTRIBUTION = "qodalis-cli-server-plugin-jobs"
#: The audit plugin (the admin plugin does not depend on it).
AUDIT_DISTRIBUTION = "qodalis-cli-server-plugin-audit"


def _features(enabled_features: list[str] | None, builder: Any) -> list[str]:
    """The enabled features (filesystem, jobs, audit, in that order): ``jobs``
    when the jobs plugin is mounted, ``audit`` when the audit plugin (which
    serves the audit log, ``GET /api/v1/qcli/audit``; the users plugin mounts
    it) is."""
    features = list(enabled_features or [])
    installed = {p.id for p in (getattr(builder, "installed_plugins", None) or [])}
    if "jobs" not in features and JOBS_DISTRIBUTION in installed:
        features.append("jobs")
    if "audit" not in features and AUDIT_DISTRIBUTION in installed:
        features.append("audit")
    return features
