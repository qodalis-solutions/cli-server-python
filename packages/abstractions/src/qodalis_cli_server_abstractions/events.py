"""Server events: the event bus contract shared by the server and its plugins.

An event has a ``type`` (``namespace:name``, e.g. ``job:completed``), a UTC
timestamp, the publisher's fields (``data``, camelCase keys) and an audience
that decides which event sockets receive it.  On the wire
(``/ws/v1/qcli/events``) it is one flat JSON object:
``{"type": ..., "timestamp": ..., **data}``; the audience is not sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Union, runtime_checkable

#: Keys of the wire object that the publisher's fields cannot replace.
RESERVED_EVENT_FIELDS = ("type", "timestamp")


@dataclass(frozen=True)
class CliEventAudience:
    """Who receives an event on the events WebSocket.

    Neither set: every socket.  ``permission``: sockets whose principal has
    it.  ``user_id``: sockets authenticated as that user.  Both: the user
    **or** anyone with the permission.  Sockets without a principal (a server
    without authentication) receive every event; in-process subscribers
    receive every event regardless of the audience."""

    permission: Optional[str] = None
    user_id: Optional[str] = None

    @property
    def is_everyone(self) -> bool:
        return self.permission is None and self.user_id is None

    @staticmethod
    def everyone() -> CliEventAudience:
        return CliEventAudience()

    @staticmethod
    def with_permission(permission: str) -> CliEventAudience:
        return CliEventAudience(permission=permission)

    @staticmethod
    def user(user_id: str) -> CliEventAudience:
        return CliEventAudience(user_id=user_id)


@dataclass
class CliEvent:
    """One published event."""

    type: str
    #: ISO 8601 UTC.
    timestamp: str
    #: The publisher's fields (camelCase), merged into the wire object.
    data: dict[str, Any] = field(default_factory=dict)
    audience: CliEventAudience = field(default_factory=CliEventAudience)

    def to_message(self) -> dict[str, Any]:
        """The flat wire object: ``type``, ``timestamp`` and the data fields."""
        message = {k: v for k, v in self.data.items() if k not in RESERVED_EVENT_FIELDS}
        return {"type": self.type, "timestamp": self.timestamp, **message}


CliEventHandler = Callable[[CliEvent], Union[Awaitable[None], None]]


@runtime_checkable
class ICliEventBus(Protocol):
    """The server's event bus (``CliServerResult.event_bus``).

    ``publish`` never fails the action that publishes: a handler's exception
    is logged and the other handlers still run.  Handlers run in subscription
    order; ``publish`` returns when they all completed."""

    async def publish(
        self,
        type: str,  # noqa: A002 - the spec's name
        data: dict[str, Any] | None = None,
        audience: CliEventAudience | None = None,
    ) -> None: ...

    def subscribe(self, pattern: str, handler: CliEventHandler) -> Callable[[], None]:
        """Call ``handler`` for every event whose type matches ``pattern``
        (``*``, ``ns:*`` or an exact type); returns the unsubscribe function."""
        ...


def cli_event_pattern_matches(type: str, pattern: str) -> bool:  # noqa: A002
    """``*`` matches every type, ``ns:*`` every type starting with ``ns:``;
    any other pattern must equal the type."""
    if pattern == "*":
        return True
    if pattern.endswith(":*"):
        return type.startswith(pattern[:-1])
    return type == pattern


__all__ = [
    "RESERVED_EVENT_FIELDS",
    "CliEvent",
    "CliEventAudience",
    "CliEventHandler",
    "ICliEventBus",
    "cli_event_pattern_matches",
]
