"""Small HTTP/ASGI helpers (kept local so the plugin does not depend on the
core server package)."""

from __future__ import annotations

import fnmatch
from typing import Any, Iterable
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Key under ``scope["state"]`` where the authenticated caller is stored
#: (shared with ``qodalis_cli.security``).
AUTH_STATE_KEY = "qcli_auth"


_DEFAULT_PORTS = {"http": 80, "https": 443}
_WS_SCHEMES = {"ws": "http", "wss": "https"}


def _scope_header(scope: Any, name: str) -> str | None:
    raw = name.encode("latin-1")
    for key, value in (scope.get("headers") if isinstance(scope, dict) else None) or []:
        if key.lower() == raw:
            return value.decode("latin-1")
    return None


def _normalize_origin(scheme: str, netloc: str) -> tuple[str, str, int] | None:
    scheme = _WS_SCHEMES.get(scheme.strip().lower(), scheme.strip().lower())
    if scheme not in _DEFAULT_PORTS or not netloc:
        return None
    try:
        parts = urlsplit(f"//{netloc.strip()}")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def is_same_origin(origin: str | None, scope: Any, trust_proxy: bool = False) -> bool:
    """True when ``origin`` equals the request's own origin (scheme + Host).

    ``ws``/``wss`` map to ``http``/``https``; default ports are normalized and
    the comparison is case-insensitive.  ``X-Forwarded-Proto`` /
    ``X-Forwarded-Host`` are honoured only when ``trust_proxy`` is on.  A
    same-origin request is never cross-site, so it always passes Origin checks.
    """
    if not origin or not isinstance(scope, dict):
        return False
    try:
        o = urlsplit(origin.strip().rstrip("/"))
    except ValueError:
        return False
    if o.path or o.query or o.fragment:
        return False
    actual = _normalize_origin(o.scheme, o.netloc)
    if actual is None:
        return False
    scheme = scope.get("scheme") or "http"
    host = _scope_header(scope, "host")
    if trust_proxy:
        fwd_proto = _scope_header(scope, "x-forwarded-proto")
        fwd_host = _scope_header(scope, "x-forwarded-host")
        if fwd_proto and fwd_proto.split(",")[0].strip():
            scheme = fwd_proto.split(",")[0].strip()
        if fwd_host and fwd_host.split(",")[0].strip():
            host = fwd_host.split(",")[0].strip()
    if not host:
        return False
    return actual == _normalize_origin(scheme, host)


def origin_allowed(
    origin: str | None,
    allowed: Iterable[str] | None,
    scope: Any = None,
    trust_proxy: bool = False,
) -> bool:
    """True when ``origin`` matches ``allowed`` (``*`` globs) or, when the
    request ``scope`` is given, equals the request's own origin."""
    if not origin:
        return False
    if scope is not None and is_same_origin(origin, scope, trust_proxy):
        return True
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
