"""Fluent builder for the audit plugin.

Usage::

    audit_plugin = CliAuditBuilder().build()       # in memory, the latest 1000 entries
    # or keep the log elsewhere:
    audit_plugin = CliAuditBuilder().set_store(MyAuditStore()).build()
    result.mount_plugin(audit_plugin)

The users plugin mounts one itself (with its own ``set_audit_store``, if any),
so a host that only mounts the users plugin keeps its audit log.  A server
serves one audit log: mounting a second audit plugin (or the same one again)
adds no routes; an explicitly set store replaces the default one, and of two
set stores the one mounted last is kept.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Request

from qodalis_cli_server_abstractions.audit import AuditEntry, IAuditStore
from qodalis_cli_server_abstractions.events import CliEventAudience, ICliEventBus

from .cli_audit_controller import create_audit_router
from .in_memory_audit_store import InMemoryAuditStore

PREFIX = "/api/v1/qcli/audit"

#: Permission of the ``audit:recorded`` event's audience.
_AUDIT_READ = "audit:read"

#: Attribute of the server result holding the audit plugin that serves it.
_SERVER_ATTR = "_qcli_audit_plugin"

#: ``authorize(request, permission)`` returns the caller or raises an
#: ``HTTPException`` (rendered as ``{"error": detail}``).
Authorizer = Callable[[Request, str], Awaitable[Any]]
StoreListener = Callable[[IAuditStore], None]


def get_audit_plugin(server: Any) -> Optional[CliAuditPlugin]:
    """The audit plugin mounted on ``server`` (a ``CliServerResult``), or ``None``."""
    return getattr(server, _SERVER_ATTR, None)


@dataclass
class CliAuditPlugin:
    """The built audit plugin: mount it with ``CliServerResult.mount_plugin``.

    ``store`` is the log; record a host's own changes with
    ``await plugin.log(AuditEntry(...))`` (stores the entry and publishes
    ``audit:recorded``).
    """

    prefix: str
    store: IAuditStore
    #: ``True`` when the store was set (not the default in-memory one).
    store_set: bool = False
    #: Checks the caller of the audit routes itself.  ``None`` (default)
    #: leaves it to the server's route policy (``audit:read`` / ``audit:clear``
    #: with the users plugin's server protection).  The users plugin sets it
    #: when it does not protect the whole server.
    authorize: Optional[Authorizer] = None
    #: The server's event bus (set when mounted): ``audit:recorded`` after
    #: each entry recorded through :meth:`log` (or by the users plugin).
    event_bus: Optional[ICliEventBus] = None
    router: APIRouter = field(init=False, repr=False)
    _listeners: list = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.router = create_audit_router(self)

    def use_store(self, store: IAuditStore, *, explicit: bool = True) -> None:
        """Keep the log in ``store`` from now on (listeners are told)."""
        self.store_set = self.store_set or explicit
        if store is self.store:
            return
        self.store = store
        for listener in list(self._listeners):
            listener(store)

    async def log(self, entry: AuditEntry) -> None:
        """Record ``entry`` in the store and publish ``audit:recorded``."""
        await self.store.log(entry)
        await self.publish_recorded(entry)

    async def publish_recorded(self, entry: AuditEntry) -> None:
        """Publish ``audit:recorded`` (audience ``audit:read``) for an entry
        already stored: its fields as the audit API returns them, the entry's
        time as the event's ``timestamp``.  Never raises."""
        if self.event_bus is None:
            return
        data = entry.to_dict()
        recorded_at = data.pop("timestamp", None)
        if isinstance(recorded_at, (int, float)):
            data["timestamp"] = datetime.fromtimestamp(recorded_at / 1000, tz=timezone.utc)
        try:
            await self.event_bus.publish(
                "audit:recorded", data, CliEventAudience(permission=_AUDIT_READ),
            )
        except Exception:  # pragma: no cover - the bus isolates its handlers
            logging.getLogger(__name__).exception("Failed to publish audit:recorded")

    def on_store_changed(self, listener: StoreListener) -> None:
        """Call ``listener(store)`` whenever the store is replaced (e.g. the
        users plugin records its events in the store of the audit plugin it
        joined)."""
        self._listeners.append(listener)

    def before_mount(self, server: Any) -> bool:
        """Hook of ``CliServerResult.mount_plugin``: ``False`` when the server
        already has an audit plugin (this one or another), which then serves
        both; the routes are added once.  Either way the plugin publishes on
        the server's event bus."""
        if self.event_bus is None:
            self.event_bus = getattr(server, "event_bus", None)
        serving = get_audit_plugin(server)
        if serving is None:
            setattr(server, _SERVER_ATTR, self)
            return True
        if serving is not self:
            serving._join(self)
        return False

    def _join(self, other: CliAuditPlugin) -> None:
        """``other`` was mounted on a server this plugin serves: one log."""
        if other.store_set:
            self.use_store(other.store)
        if self.authorize is None and other.authorize is not None:
            self.authorize = other.authorize
        # ``other`` (and whoever follows it, e.g. the users plugin) records
        # in this plugin's store, now and after it is replaced.
        other.use_store(self.store, explicit=False)
        self.on_store_changed(lambda store: other.use_store(store, explicit=False))


class CliAuditBuilder:
    """Fluent builder for the audit plugin."""

    def __init__(self) -> None:
        self._store: IAuditStore | None = None

    def set_store(self, store: IAuditStore) -> CliAuditBuilder:
        """Keep the log in ``store`` (default: in memory, the latest 1000
        entries, failed sign-ins apart, at most 200)."""
        self._store = store
        return self

    def build(self) -> CliAuditPlugin:
        return CliAuditPlugin(
            prefix=PREFIX,
            store=self._store or InMemoryAuditStore(),
            store_set=self._store is not None,
        )
