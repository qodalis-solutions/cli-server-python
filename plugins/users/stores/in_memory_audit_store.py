"""In-memory implementation of IAuditStore."""

from __future__ import annotations

from ..models import AuditEntry, AuditQueryOptions
from .interfaces import IAuditStore

MAX_ENTRIES = 1000


class InMemoryAuditStore(IAuditStore):
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    async def log(self, entry: AuditEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > MAX_ENTRIES:
            self._entries.pop(0)

    async def get_entries(self, options: AuditQueryOptions | None = None) -> list[AuditEntry]:
        # Return newest-first
        result = list(reversed(self._entries))

        if options:
            if options.event:
                result = [e for e in result if e.event == options.event]
            if options.username:
                result = [e for e in result if e.username == options.username]
            if options.limit is not None and options.limit > 0:
                result = result[: options.limit]

        return result

    async def clear(self) -> None:
        self._entries.clear()
