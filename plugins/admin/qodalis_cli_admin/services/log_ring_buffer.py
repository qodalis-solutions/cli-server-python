"""Ring buffer for log entries with optional broadcast support."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable


@dataclass
class LogEntry:
    """A single log entry."""

    timestamp: str
    level: str
    message: str
    logger_name: str = ""


BroadcastFn = Callable[[str], Awaitable[None]]

_LEVEL_MAP = {
    "WARNING": "WARN",
}


def _normalize_level(level: str) -> str:
    """Normalize Python log level names to SPA-expected short forms."""
    return _LEVEL_MAP.get(level.upper(), level.upper())


class LogRingBuffer:
    """Fixed-size ring buffer that stores log entries.

    Optionally captures records from Python's :mod:`logging` module via
    :meth:`install_handler`.
    """

    def __init__(
        self,
        max_size: int = 1000,
        broadcast_fn: BroadcastFn | None = None,
    ) -> None:
        self._buffer: deque[LogEntry] = deque(maxlen=max_size)
        self._broadcast_fn = broadcast_fn
        self._handler: _RingBufferHandler | None = None

    # -- public API -----------------------------------------------------------

    def add(self, entry: LogEntry) -> None:
        """Append a log entry to the buffer."""
        self._buffer.append(entry)

    def query(
        self,
        *,
        level: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return matching entries and the total count (before pagination)."""
        entries = list(self._buffer)

        if level:
            level_upper = level.upper()
            # Support filtering by SPA-form "WARN" matching Python's "WARNING"
            match_levels = {level_upper}
            if level_upper == "WARN":
                match_levels.add("WARNING")
            entries = [e for e in entries if e.level in match_levels]

        if search:
            search_lower = search.lower()
            entries = [e for e in entries if search_lower in e.message.lower()]

        total = len(entries)
        page = entries[offset: offset + limit]

        return (
            [
                {
                    "timestamp": e.timestamp,
                    "level": _normalize_level(e.level),
                    "message": e.message,
                    "source": e.logger_name,
                }
                for e in page
            ],
            total,
        )

    # -- logging integration --------------------------------------------------

    def install_handler(self, logger_name: str = "") -> None:
        """Install a :class:`logging.Handler` that feeds into this buffer."""
        if self._handler is not None:
            return
        self._handler = _RingBufferHandler(self)
        logging.getLogger(logger_name).addHandler(self._handler)

    def uninstall_handler(self, logger_name: str = "") -> None:
        """Remove the previously installed handler."""
        if self._handler is None:
            return
        logging.getLogger(logger_name).removeHandler(self._handler)
        self._handler = None


class _RingBufferHandler(logging.Handler):
    """Logging handler that pushes records into a :class:`LogRingBuffer`."""

    def __init__(self, ring: LogRingBuffer) -> None:
        super().__init__()
        self._ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level=record.levelname,
            message=self.format(record),
            logger_name=record.name,
        )
        self._ring.add(entry)
