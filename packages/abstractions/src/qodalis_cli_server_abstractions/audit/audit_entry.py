"""The audit log's entries and the query the audit API passes to a store."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict


def _new_id() -> str:
    return str(uuid.uuid4())


def _now_ms() -> int:
    return int(time.time() * 1000)


class AuditMetadata(TypedDict, total=False):
    """Where an action came from: the caller's address and session.  Stored in
    :attr:`AuditEntry.metadata` (a plain dict, camelCase keys as returned by
    the API); a host may add keys of its own."""

    remoteAddress: Optional[str]
    sessionId: Optional[str]


@dataclass
class AuditEntry:
    """One entry of the audit log: who did what (``event``), to whom
    (``username``) or what (``target``), and when (Unix time in ms)."""

    id: str = field(default_factory=_new_id)
    timestamp: int = field(default_factory=_now_ms)
    event: str = ""
    #: The user the event is about.
    username: str = ""
    details: str | None = None
    #: The user who performed the action.
    actor: str | None = None
    #: Where it came from (:class:`AuditMetadata`: ``remoteAddress``, ``sessionId``, ...).
    metadata: dict[str, Any] | None = None
    user_id: str | None = None
    actor_id: str | None = None
    #: What the event is about when it is not a user: a group
    #: (``group:admins``), a job, or a host's own object such as a role.
    target: str | None = None
    #: The host's own values for this entry (e.g. the old and new value of a
    #: setting), returned by the audit API.  Values must serialize to JSON.
    properties: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """The camelCase JSON shape of the audit API (unset fields omitted)."""
        result: dict[str, Any] = {
            "id": self.id,
            "timestamp": self.timestamp,
            "event": self.event,
            "username": self.username,
        }
        if self.user_id is not None:
            result["userId"] = self.user_id
        if self.actor_id is not None:
            result["actorId"] = self.actor_id
        if self.details is not None:
            result["details"] = self.details
        if self.actor is not None:
            result["actor"] = self.actor
        if self.target is not None:
            result["target"] = self.target
        if self.properties is not None:
            result["properties"] = self.properties
        if self.metadata is not None:
            result["metadata"] = self.metadata
        return result


@dataclass
class AuditQueryOptions:
    """Filters and paging of :meth:`IAuditStore.get_entries` (newest first)."""

    #: The most entries to return (``None``: all).
    limit: int | None = None
    #: The event (exact, case-insensitive).
    event: str | None = None
    #: The user the events are about (exact, case-insensitive).
    username: str | None = None
    #: The user who performed the actions (exact, case-insensitive).
    actor: str | None = None
    #: Entries to skip, for paging (newest first).
    offset: int | None = None
