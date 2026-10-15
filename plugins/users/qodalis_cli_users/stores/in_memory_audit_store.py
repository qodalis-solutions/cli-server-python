"""In-memory implementation of IAuditStore.

Keeps the last 1000 entries, with failed logins capped separately (last 200)
so a flood of failed attempts cannot evict other history.
"""

from __future__ import annotations

import itertools
from collections import deque

from ..models import AuditEntry, AuditQueryOptions
from .interfaces import IAuditStore

MAX_ENTRIES = 1000
MAX_FAILED_LOGIN_ENTRIES = 200
FAILED_LOGIN_EVENT = "login_failed"


class InMemoryAuditStore(IAuditStore):
    def __init__(
        self,
        max_entries: int = MAX_ENTRIES,
        max_failed_login_entries: int = MAX_FAILED_LOGIN_ENTRIES,
    ) -> None:
        self._seq = itertools.count()
        self._entries: deque[tuple[int, AuditEntry]] = deque(maxlen=max_entries)
        self._failed: deque[tuple[int, AuditEntry]] = deque(maxlen=max_failed_login_entries)

    async def log(self, entry: AuditEntry) -> None:
        target = self._failed if entry.event == FAILED_LOGIN_EVENT else self._entries
        target.append((next(self._seq), entry))

    async def get_entries(self, options: AuditQueryOptions | None = None) -> list[AuditEntry]:
        merged = sorted([*self._entries, *self._failed], key=lambda x: x[0], reverse=True)
        result = [e for _, e in merged]  # newest first
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
        self._failed.clear()
