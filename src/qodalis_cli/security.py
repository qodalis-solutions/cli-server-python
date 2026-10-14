"""Security helpers shared by the core server and auth plugins.

The core server has no notion of users.  An auth plugin (e.g. the users
plugin) authenticates requests in an ASGI middleware and stores the caller in
``scope["state"]["qcli_auth"]``; the object only needs a ``can(permission)``
method.  When no auth context is present (no auth plugin), everything behaves
as before.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any, Iterable, Protocol

AUTH_STATE_KEY = "qcli_auth"

#: ``LogRecord`` attribute marking records that must never be streamed to
#: log sockets or buffers (e.g. a generated initial password).
SENSITIVE_LOG_ATTR = "qcli_sensitive"


class AuthContextLike(Protocol):
    def can(self, permission: str) -> bool: ...


def get_auth_context(scope_or_conn: Any) -> AuthContextLike | None:
    """Return the auth context attached to a request/websocket scope."""
    scope = getattr(scope_or_conn, "scope", scope_or_conn)
    state = scope.get("state") if isinstance(scope, dict) else None
    if isinstance(state, dict):
        return state.get(AUTH_STATE_KEY)
    return getattr(state, AUTH_STATE_KEY, None) if state is not None else None


def set_auth_context(scope: dict, ctx: Any) -> None:
    state = scope.get("state")
    if state is None:
        state = {}
        scope["state"] = state
    if isinstance(state, dict):
        state[AUTH_STATE_KEY] = ctx
    else:  # pragma: no cover - defensive
        setattr(state, AUTH_STATE_KEY, ctx)


def missing_processor_permissions(ctx: AuthContextLike | None, *processors: Any) -> list[str]:
    """Permissions declared by ``processors`` that ``ctx`` does not hold.

    Returns ``[]`` when there is no auth context (unprotected server)."""
    if ctx is None:
        return []
    missing: list[str] = []
    for proc in processors:
        if proc is None:
            continue
        for perm in getattr(proc, "required_permissions", None) or []:
            if not ctx.can(perm) and perm not in missing:
                missing.append(perm)
    return missing


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------

def origin_allowed(origin: str | None, allowed: Iterable[str] | None) -> bool:
    """True when ``origin`` matches one of ``allowed`` (``*`` globs allowed,
    e.g. ``http://127.0.0.1:*``)."""
    if not origin:
        return False
    origin = origin.rstrip("/").lower()
    for pattern in allowed or []:
        pattern = pattern.rstrip("/").lower()
        if pattern == "*" or pattern == origin or fnmatch.fnmatchcase(origin, pattern):
            return True
    return False


def origins_regex(allowed: Iterable[str]) -> str | None:
    """Regex for CORSMiddleware ``allow_origin_regex`` from glob patterns."""
    globs = [o for o in allowed if "*" in o and o != "*"]
    if not globs:
        return None
    return "|".join(fnmatch.translate(g.rstrip("/")) for g in globs)


class WebSocketOriginMiddleware:
    """Rejects WebSocket upgrades whose ``Origin`` is not allowed (close 4403).

    A missing ``Origin`` is accepted: browsers always send one, so its absence
    means a non-browser client (which still needs a ticket on a protected
    server). Matches the node and .NET servers.
    """

    def __init__(self, app: Any, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            origin = _header(scope, b"origin")
            if origin is not None and not origin_allowed(origin, self.allowed_origins):
                await close_websocket(receive, send, 4403, "origin not allowed")
                return
        await self.app(scope, receive, send)


def _header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def close_websocket(receive: Any, send: Any, code: int, reason: str = "") -> None:
    """Complete the handshake and close with ``code`` so clients see it."""
    message = await receive()
    if message.get("type") != "websocket.connect":
        return
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": code, "reason": reason})


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

_REDACTIONS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(token|ticket|password|secret|cmd)=([^&\s]+)"), r"\1=[redacted]"),
    (re.compile(r'(?i)"(token|ticket|password|currentPassword|newPassword)"\s*:\s*"[^"]*"'), r'"\1": "[redacted]"'),
]


def redact(message: str) -> str:
    """Mask bearer tokens, tickets and passwords in a log message."""
    for pattern, repl in _REDACTIONS:
        message = pattern.sub(repl, message)
    return message


def is_sensitive_record(record: logging.LogRecord) -> bool:
    return bool(getattr(record, SENSITIVE_LOG_ATTR, False))
