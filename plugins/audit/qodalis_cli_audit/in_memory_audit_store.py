"""In-memory implementation of :class:`IAuditStore`.

Keeps the latest 1000 entries; failed sign-ins (``login_failed``) are capped
separately (the latest 200) so a flood of them cannot evict the rest.
"""

from __future__ import annotations

import itertools
from collections import deque

from qodalis_cli_server_abstractions.audit import AuditEntry, AuditQueryOptions, IAuditStore

MAX_ENTRIES = 1000
MAX_FAILED_LOGIN_ENTRIES = 200
#: The event of a failed sign-in (users plugin), kept apart from the rest.
FAILED_LOGIN_EVENT = "login_failed"


class InMemoryAuditStore(IAuditStore):
    def __init__(
        self,
        max_entries: int = MAX_ENTRIES,
        max_failed_login_entries: int = MAX_FAILED_LOGIN_ENTRIES,
    ) -> None:
        self.max_entries = max_entries if max_entries > 0 else MAX_ENTRIES
        self.max_failed_login_entries = (
            max_failed_login_entries if max_failed_login_entries > 0 else MAX_FAILED_LOGIN_ENTRIES
        )
        self._seq = itertools.count()
        self._entries: deque[tuple[int, AuditEntry]] = deque(maxlen=self.max_entries)
        self._failed: deque[tuple[int, AuditEntry]] = deque(maxlen=self.max_failed_login_entries)

    async def log(self, entry: AuditEntry) -> None:
        target = self._failed if entry.event == FAILED_LOGIN_EVENT else self._entries
        target.append((next(self._seq), entry))

    async def get_entries(self, options: AuditQueryOptions | None = None) -> list[AuditEntry]:
        # Newest recorded first.
        merged = sorted([*self._entries, *self._failed], key=lambda item: item[0], reverse=True)
        result = [entry for _, entry in merged]
        if options:
            if options.event:
                event = options.event.lower()
                result = [e for e in result if e.event.lower() == event]
            if options.username:
                username = options.username.lower()
                result = [e for e in result if e.username.lower() == username]
            if options.actor:
                actor = options.actor.lower()
                result = [e for e in result if (e.actor or "").lower() == actor]
            if options.offset is not None and options.offset > 0:
                result = result[options.offset:]
            if options.limit is not None and options.limit >= 0:
                result = result[: options.limit]
        return result

    async def prune(self, before: int) -> int:
        removed = 0
        for entries in (self._entries, self._failed):
            kept = [item for item in entries if item[1].timestamp >= before]
            removed += len(entries) - len(kept)
            entries.clear()
            entries.extend(kept)
        return removed

    async def clear(self) -> None:
        self._entries.clear()
        self._failed.clear()
