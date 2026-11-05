"""In-memory command usage counters (``GET /api/v1/qcli/commands/stats``)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _iso_ms(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


@dataclass
class _Usage:
    known: bool
    executions: int = 0
    failures: int = 0
    total_duration: float = 0.0
    last_executed_at: datetime | None = None
    last_failed_at: datetime | None = None


class CliCommandUsageTracker:
    """Counts command executions per root command since the server started:
    executions, failures (exit code != 0 or an exception), total duration
    (ms) and the last execution and failure times.  Thread-safe."""

    def __init__(self) -> None:
        self.since = datetime.now(timezone.utc)
        self._lock = threading.Lock()
        self._usage: dict[str, _Usage] = {}

    def record(
        self,
        command: str,
        *,
        known: bool,
        duration_ms: float,
        failed: bool,
        at: datetime | None = None,
    ) -> None:
        """Count one execution of ``command`` (its root name, lower-cased)."""
        name = (command or "").strip().lower()
        if not name:
            return
        when = at or datetime.now(timezone.utc)
        with self._lock:
            usage = self._usage.get(name)
            if usage is None:
                usage = self._usage[name] = _Usage(known=known)
            usage.known = usage.known or known
            usage.executions += 1
            usage.total_duration += max(duration_ms, 0.0)
            usage.last_executed_at = when
            if failed:
                usage.failures += 1
                usage.last_failed_at = when

    def snapshot(self) -> dict[str, Any]:
        """The ``GET /commands/stats`` body: by executions descending, then name."""
        with self._lock:
            items = [(name, _Usage(**vars(u))) for name, u in self._usage.items()]
        items.sort(key=lambda item: (-item[1].executions, item[0]))
        return {
            "since": _iso_ms(self.since),
            "commands": [
                {
                    "command": name,
                    "known": u.known,
                    "executions": u.executions,
                    "failures": u.failures,
                    "avgDuration": u.total_duration / u.executions if u.executions else None,
                    "lastExecutedAt": _iso_ms(u.last_executed_at) if u.last_executed_at else None,
                    "lastFailedAt": _iso_ms(u.last_failed_at) if u.last_failed_at else None,
                }
                for name, u in items
            ],
        }
