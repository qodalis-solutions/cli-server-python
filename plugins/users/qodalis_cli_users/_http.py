"""Small HTTP/ASGI helpers (kept local so the plugin does not depend on the
core server package)."""

from __future__ import annotations

import fnmatch
from typing import Any, Iterable

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Key under ``scope["state"]`` where the authenticated caller is stored
#: (shared with ``qodalis_cli.security``).
AUTH_STATE_KEY = "qcli_auth"


def origin_allowed(origin: str | None, allowed: Iterable[str] | None) -> bool:
    if not origin:
        return False
    origin = origin.rstrip("/").lower()
    for pattern in allowed or []:
        pattern = pattern.rstrip("/").lower()
        if pattern == "*" or pattern == origin or fnmatch.fnmatchcase(origin, pattern):
            return True
    return False


def has_bearer(headers: Any) -> bool:
    auth = headers.get("authorization", "") or ""
    return auth[:7].lower() == "bearer " and bool(auth[7:].strip())


def bearer_token(headers: Any) -> str | None:
    auth = headers.get("authorization", "") or ""
    if auth[:7].lower() != "bearer ":
        return None
    token = auth[7:].strip()
    return token or None


def get_scope_auth(scope: dict) -> Any:
    state = scope.get("state")
    if isinstance(state, dict):
        return state.get(AUTH_STATE_KEY)
    return getattr(state, AUTH_STATE_KEY, None) if state is not None else None


def set_scope_auth(scope: dict, ctx: Any) -> None:
    state = scope.get("state")
    if state is None:
        state = {}
        scope["state"] = state
    if isinstance(state, dict):
        state[AUTH_STATE_KEY] = ctx
    else:
        setattr(state, AUTH_STATE_KEY, ctx)
