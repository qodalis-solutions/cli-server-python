"""Ring buffer for log entries with optional broadcast support."""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Awaitable


@dataclass
class LogEntry:
    """A single log entry."""

    timestamp: str
    level: str
    message: str
    logger_name: str = ""


BroadcastFn = Callable[[str], Awaitable[None]]

_REDACTIONS = [
    # Also the URL-encoded ``Bearer%20<token>`` form (``_header_Authorization`` query).
    (re.compile(r"(?i)(bearer(?:\s+|%20))[A-Za-z0-9._~+/=%-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(token|ticket|password|secret|cmd)=([^&\s]+)"), r"\1=[redacted]"),
    (re.compile(r'(?i)"(token|ticket|password|currentPassword|newPassword)"\s*:\s*"[^"]*"'), r'"\1": "[redacted]"'),
]


def redact(message: str) -> str:
    """Mask bearer tokens, tickets and passwords in a log message."""
    for pattern, repl in _REDACTIONS:
        message = pattern.sub(repl, message)
    return message

_LEVEL_MAP = {
    "WARNING": "WARN",
}


#: The columns of ``GET /logs/histogram``.
HISTOGRAM_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

_HISTOGRAM_LEVEL_MAP = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "ERROR",
    "FATAL": "ERROR",
}


def histogram_level(level: str | None) -> str | None:
    """The histogram column of a level (``WARNING`` -> ``WARN``, ``CRITICAL``
    -> ``ERROR``), ``None`` for an unknown level."""
    return _HISTOGRAM_LEVEL_MAP.get((level or "").strip().upper())


def iso_ms(value: datetime) -> str:
    """``2026-11-05T21:00:00.000Z`` (UTC, milliseconds)."""
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _entry_time(entry: LogEntry) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


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

    def add(self, entry: LogEntry) -> None:
        """Append a log entry to the buffer."""
        entry.level = (entry.level or "INFO").upper()
        self._buffer.append(entry)

    def query(
        self,
        *,
        level: str | None = None,
        search: str | None = None,
        service: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return matching entries and the total count (before pagination).

        ``level`` is one level or comma-separated levels (``DEBUG``, ``INFO``,
        ``WARN``, ``ERROR``; Python's names work too), ``search`` a literal
        case-insensitive text in the message or the source, ``service`` the
        service that wrote the entries (the in-memory buffer has none, so
        any service matches nothing); ``since``/``until`` keep the entries
        with ``since <= timestamp < until``."""
        entries = self._filter(level=level, search=search, service=service)
        if since is not None or until is not None:
            entries = [
                e for e in entries
                if (stamp := _entry_time(e)) is not None
                and (since is None or stamp >= since)
                and (until is None or stamp < until)
            ]
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

    def histogram(
        self,
        since: datetime,
        until: datetime,
        buckets: int,
        *,
        levels: list[str] | None = None,
        search: str | None = None,
        service: str | None = None,
    ) -> dict[str, Any]:
        """Entries in ``[since, until)`` counted per level in ``buckets``
        equal time columns (``GET /logs/histogram``)."""
        since_ms = since.timestamp() * 1000
        bucket_ms = (until.timestamp() * 1000 - since_ms) / buckets
        columns: list[dict[str, Any]] = [
            {
                "start": iso_ms(since + timedelta(milliseconds=i * bucket_ms)),
                **{name: 0 for name in HISTOGRAM_LEVELS},
            }
            for i in range(buckets)
        ]
        totals = {name: 0 for name in HISTOGRAM_LEVELS}
        wanted = set(levels) if levels else None
        for entry in self._filter(search=search, service=service):
            name = histogram_level(entry.level)
            if name is None or (wanted is not None and name not in wanted):
                continue
            stamp = _entry_time(entry)
            if stamp is None or not since <= stamp < until:
                continue
            index = min(max(int((stamp.timestamp() * 1000 - since_ms) // bucket_ms), 0), buckets - 1)
            columns[index][name] += 1
            totals[name] += 1
        return {
            "from": iso_ms(since),
            "to": iso_ms(until),
            "bucketMs": int(bucket_ms) if float(bucket_ms).is_integer() else bucket_ms,
            "buckets": columns,
            "totals": totals,
            "store": "memory",
        }

    def _filter(
        self,
        *,
        level: str | None = None,
        search: str | None = None,
        service: str | None = None,
    ) -> list[LogEntry]:
        entries = list(self._buffer)
        if service:
            return []

        if level:
            match_levels: set[str] = set()
            for name in level.split(","):
                name = name.strip().upper()
                if not name:
                    continue
                match_levels.add(name)
                # Support filtering by SPA-form "WARN" matching Python's "WARNING"
                if name == "WARN":
                    match_levels.add("WARNING")
            if match_levels:
                entries = [e for e in entries if e.level in match_levels]

        if search:
            search_lower = search.lower()
            entries = [
                e for e in entries
                if search_lower in e.message.lower() or search_lower in (e.logger_name or "").lower()
            ]
        return entries

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
        """Convert a log record to a LogEntry and append it to the ring buffer.

        Records flagged ``qcli_sensitive`` are skipped and tokens/passwords
        are redacted."""
        if getattr(record, "qcli_sensitive", False):
            return
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level=record.levelname,
            message=redact(self.format(record)),
            logger_name=record.name,
        )
        self._ring.add(entry)
