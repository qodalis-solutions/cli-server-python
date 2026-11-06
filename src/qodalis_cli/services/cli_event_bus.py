"""The server's in-process event bus (``CliServerResult.event_bus``)."""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from qodalis_cli_server_abstractions.events import (
    CliEvent,
    CliEventAudience,
    CliEventHandler,
    cli_event_pattern_matches,
)

logger = logging.getLogger(__name__)


def utc_timestamp(value: datetime | None = None) -> str:
    """ISO 8601 UTC with milliseconds, e.g. ``2026-11-06T10:15:00.000Z``."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CliEventBus:
    """Publishes events to subscribers (implements ``ICliEventBus``).

    The events WebSocket is one subscriber of ``*`` that applies each event's
    audience; plugins and host code subscribe to react to events in code.
    A handler's exception is logged and never reaches the publisher."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[str, CliEventHandler]] = []

    async def publish(
        self,
        type: str,  # noqa: A002 - the spec's name
        data: dict[str, Any] | None = None,
        audience: CliEventAudience | None = None,
    ) -> None:
        fields = dict(data or {})
        fields.pop("type", None)
        # The publisher may date the event itself; otherwise it is now.
        stamp = fields.pop("timestamp", None)
        if isinstance(stamp, datetime):
            stamp = utc_timestamp(stamp)
        elif not isinstance(stamp, str) or not stamp:
            stamp = utc_timestamp()
        event = CliEvent(type=type, timestamp=stamp, data=fields, audience=audience or CliEventAudience())

        for pattern, handler in list(self._subscriptions):
            if not cli_event_pattern_matches(type, pattern):
                continue
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Event handler for '%s' failed on %s", pattern, type)

    def subscribe(self, pattern: str, handler: CliEventHandler) -> Callable[[], None]:
        entry = (pattern, handler)
        self._subscriptions.append(entry)

        def unsubscribe() -> None:
            # Identity, not equality: the same handler may be subscribed twice.
            for i, sub in enumerate(self._subscriptions):
                if sub is entry:
                    del self._subscriptions[i]
                    return

        return unsubscribe
