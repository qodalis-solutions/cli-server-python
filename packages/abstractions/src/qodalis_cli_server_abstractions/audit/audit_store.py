"""The audit log's store contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .audit_entry import AuditEntry, AuditQueryOptions


class IAuditStore(ABC):
    """Persistence of the audit log.  The audit plugin
    (``qodalis-cli-server-plugin-audit``) keeps it in memory by default; a
    host keeps it elsewhere by implementing this and setting it on the plugin
    (or on the users plugin, which does the same)."""

    @abstractmethod
    async def log(self, entry: AuditEntry) -> None:
        """Records an entry."""

    @abstractmethod
    async def get_entries(self, options: AuditQueryOptions | None = None) -> list[AuditEntry]:
        """Entries newest first, filtered and paged by ``options``."""

    @abstractmethod
    async def clear(self) -> None:
        """Deletes every entry."""

    async def prune(self, before: int) -> int:
        """Deletes the entries recorded before ``before`` (Unix time in ms);
        returns how many.  Run it on a schedule to keep the log bounded.  The
        default keeps everything and returns 0."""
        return 0
