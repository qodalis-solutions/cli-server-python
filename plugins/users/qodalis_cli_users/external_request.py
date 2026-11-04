"""Snapshot of the request an external auth adapter accepted.

Sockets opened by adapter identities have no stored session; they are
re-checked by running the adapter again on a fresh request rebuilt from this
snapshot: the request that authenticated them (the ``POST
/sessions/ws-ticket`` request when a ticket was used).  Only the request line,
headers (cookies included), client/server addresses and the app are kept;
state set by middleware is not.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

#: ASGI scope keys copied into the snapshot.
_SCOPE_KEYS = (
    "type",
    "asgi",
    "http_version",
    "method",
    "scheme",
    "server",
    "client",
    "root_path",
    "path",
    "raw_path",
    "query_string",
    "app",
)


class ExternalRequestSnapshot:
    """An immutable copy of an HTTP (or WebSocket) request's scope."""

    __slots__ = ("_scope",)

    def __init__(self, scope: dict[str, Any]) -> None:
        self._scope = scope

    @classmethod
    def capture(cls, request: Any) -> ExternalRequestSnapshot | None:
        """Snapshot ``request`` (anything with an ASGI ``scope``), or ``None``."""
        scope = getattr(request, "scope", None)
        if not isinstance(scope, dict) or scope.get("type") not in ("http", "websocket"):
            return None
        copied = {key: scope[key] for key in _SCOPE_KEYS if key in scope}
        copied["headers"] = [(bytes(k), bytes(v)) for k, v in scope.get("headers") or []]
        return cls(copied)

    def to_request(self) -> Request:
        """A new request from the snapshot (empty ``state``, no body)."""
        scope = dict(self._scope)
        scope["headers"] = list(self._scope["headers"])
        scope["state"] = {}
        return Request(scope)


__all__ = ["ExternalRequestSnapshot"]
