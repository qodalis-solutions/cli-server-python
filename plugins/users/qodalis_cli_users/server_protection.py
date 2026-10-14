"""Server-wide protection (security contract B1/B2/B4/B9).

An ASGI middleware installed by the users plugin that requires an
authenticated users session for every CLI server HTTP route and WebSocket,
except login, version discovery and health endpoints.  The authenticated
caller is stored in ``scope["state"]["qcli_auth"]`` so the core server can
enforce processor ``required_permissions`` and scope events to users.

WebSockets authenticate with a single-use ticket (``?ticket=``) obtained from
``POST /api/v1/qcli/sessions/ws-ticket``; bearer tokens in query strings are
not accepted.  Failures close the socket with 4401 (unauthenticated) or 4403
(forbidden).
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from starlette.requests import Request

from ._http import SAFE_METHODS, set_scope_auth
from .errors import UsersApiError
from .middleware.auth_middleware import authenticate_request
from .services.auth_service import PASSWORD_CHANGE_REQUIRED, AuthContext, AuthService

Rule = tuple[re.Pattern[str], frozenset[str] | None, str | None]

#: Always public (method set ``None`` = any method).
PUBLIC_PATHS: list[tuple[re.Pattern[str], frozenset[str] | None]] = [
    (re.compile(r"^/api/v1/qcli/sessions/?$"), frozenset({"POST"})),
    (re.compile(r"^/api/(qcli|cli|v1/qcli|v1/cli)/versions?/?$"), frozenset({"GET", "HEAD"})),
    (re.compile(r"^/api/(qcli|cli|v1/qcli|v1/cli)/capabilities/?$"), frozenset({"GET", "HEAD"})),
    (re.compile(r"^/(api/)?(qcli/)?health(z|/.*)?$"), None),
]

#: Routes of the users plugin itself — they authenticate in their own
#: dependencies (with CSRF and password-change gating).
SELF_PROTECTED = re.compile(r"^/api/v1/qcli/(sessions|users|groups|audit)(/.*)?$")

#: HTTP permission rules, first match wins: (path, methods or None, permission).
HTTP_RULES: list[Rule] = [
    (re.compile(r"^/api/(v1/)?qcli/fs/(ls|cat|stat|download)/?$"), None, "files:read"),
    (re.compile(r"^/api/(v1/)?qcli/fs(/.*)?$"), None, "files:write"),
    (re.compile(r"^/api/(v1/)?qcli/data-explorer(/.*)?$"), None, "data:query"),
    (re.compile(r"^/api/(v1/)?qcli/jobs(/.*)?$"), frozenset({"GET", "HEAD"}), "jobs:read"),
    (re.compile(r"^/api/(v1/)?qcli/jobs(/.*)?$"), None, "jobs:manage"),
    (re.compile(r"^/api/(v1/)?qcli/execute(/stream)?/?$"), None, "commands:execute"),
]

WS_RULES: list[tuple[re.Pattern[str], str | None]] = [
    (re.compile(r"^/ws/(v1/)?qcli/events/?$"), None),
    (re.compile(r"^/ws/(v1/)?qcli/logs/?$"), "logs:read"),
    (re.compile(r"^/ws/(v1/)?qcli/shell/?$"), "shell:execute"),
]


def http_permission(path: str, method: str) -> str | None:
    for pattern, methods, permission in HTTP_RULES:
        if pattern.match(path) and (methods is None or method in methods):
            return permission
    return None


def ws_permission(path: str) -> str | None:
    for pattern, permission in WS_RULES:
        if pattern.match(path):
            return permission
    return None


class ServerProtectionMiddleware:
    """Pure ASGI middleware enforcing B1 for HTTP and WebSocket scopes."""

    def __init__(
        self,
        app: Any,
        auth_service: AuthService,
        ensure_ready: Callable[[], Awaitable[None]] | None = None,
        extra_public: list[tuple[str, frozenset[str] | None]] | None = None,
    ) -> None:
        self.app = app
        self.auth = auth_service
        self.ensure_ready = ensure_ready
        self.extra_public = extra_public if extra_public is not None else []

    # ------------------------------------------------------------------

    def _is_public(self, path: str, method: str) -> bool:
        for pattern, methods in PUBLIC_PATHS:
            if pattern.match(path) and (methods is None or method in methods):
                return True
        for public_path, methods in self.extra_public:
            if path.rstrip("/") == public_path.rstrip("/") and (methods is None or method in methods):
                return True
        return False

    @staticmethod
    def _protected(path: str) -> bool:
        return path.startswith("/api/") or path.startswith("/ws/")

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    # ------------------------------------------------------------------

    async def _http(self, scope: dict, receive: Any, send: Any) -> None:
        path: str = scope["path"]
        method: str = scope["method"].upper()
        if (
            method == "OPTIONS"
            or not self._protected(path)
            or self._is_public(path, method)
            or SELF_PROTECTED.match(path)
        ):
            await self.app(scope, receive, send)
            return
        if self.ensure_ready is not None:
            await self.ensure_ready()
        try:
            ctx = await authenticate_request(self.auth, Request(scope))
            if ctx.must_change_password:
                raise UsersApiError(403, PASSWORD_CHANGE_REQUIRED)
            permission = http_permission(path, method)
            if permission and not ctx.can(permission):
                raise UsersApiError(403, f"missing permission: {permission}")
        except UsersApiError as exc:
            await _send_json(send, exc.status_code, {"error": exc.message})
            return
        set_scope_auth(scope, ctx)
        await self.app(scope, receive, send)

    async def _websocket(self, scope: dict, receive: Any, send: Any) -> None:
        path: str = scope["path"]
        if not self._protected(path):
            await self.app(scope, receive, send)
            return
        if self.ensure_ready is not None:
            await self.ensure_ready()
        ticket = _query_param(scope, "ticket")
        ctx = await self.auth.consume_ticket(ticket)
        if ctx is None:
            await _close(receive, send, 4401, "unauthorized")
            return
        permission = ws_permission(path)
        if ctx.must_change_password or (permission and not ctx.can(permission)):
            await _close(receive, send, 4403, "forbidden")
            return
        set_scope_auth(scope, ctx)
        await self.app(scope, receive, send)


def _query_param(scope: dict, name: str) -> str | None:
    from urllib.parse import parse_qs

    raw = scope.get("query_string", b"").decode("latin-1")
    values = parse_qs(raw).get(name)
    return values[0] if values else None


async def _send_json(send: Any, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def _close(receive: Any, send: Any, code: int, reason: str) -> None:
    message = await receive()
    if message.get("type") != "websocket.connect":
        return
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": code, "reason": reason})


__all__ = ["AuthContext", "ServerProtectionMiddleware", "http_permission", "ws_permission", "SAFE_METHODS"]
