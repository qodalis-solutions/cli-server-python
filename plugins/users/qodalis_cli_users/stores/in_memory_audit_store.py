"""In-memory implementation of IAuditStore (keeps the last 1000 entries)."""

from __future__ import annotations

from collections import deque

from ..models import AuditEntry, AuditQueryOptions
from .interfaces import IAuditStore

MAX_ENTRIES = 1000


class InMemoryAuditStore(IAuditStore):
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)

    async def log(self, entry: AuditEntry) -> None:
        self._entries.append(entry)

    async def get_entries(self, options: AuditQueryOptions | None = None) -> list[AuditEntry]:
        result = list(reversed(self._entries))  # newest first
        if options:
            if options.event:
                ev = options.event.lower()
                result = [e for e in result if e.event.lower() == ev]
            if options.username:
                un = options.username.lower()
                result = [e for e in result if e.username.lower() == un]
            if options.limit is not None and options.limit > 0:
                result = result[: options.limit]
        return result

    async def clear(self) -> None:
        self._entries.clear()
