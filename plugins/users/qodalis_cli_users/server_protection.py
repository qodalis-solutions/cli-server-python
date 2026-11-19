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

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request

from ._http import SAFE_METHODS, bearer_token, set_scope_auth
from .errors import UsersApiError
from .middleware.auth_middleware import authenticate_request
from .services.auth_service import (
    API_TOKEN_PREFIX,
    HOST_TOKENS_CANNOT_USE_CLI_ROUTES,
    hash_token,
    PASSWORD_CHANGE_REQUIRED,
    AuthContext,
    AuthService,
)

Rule = tuple[re.Pattern[str], frozenset[str] | None, str | None]

#: Always public (method set ``None`` = any method).
PUBLIC_PATHS: list[tuple[re.Pattern[str], frozenset[str] | None]] = [
    (re.compile(r"^/api/v1/qcli/sessions/?$"), frozenset({"POST"})),
    (re.compile(r"^/api/(qcli|cli|v1/qcli|v1/cli)/versions?/?$"), frozenset({"GET", "HEAD"})),
    (re.compile(r"^/api/(qcli|cli|v1/qcli|v1/cli)/capabilities/?$"), frozenset({"GET", "HEAD"})),
    (re.compile(r"^/(api/)?(qcli/)?health(z|/.*)?$"), None),
    # Admin dashboard: which sign-in to show (password form or the host's).
    (re.compile(r"^/api/v1/qcli/auth/mode/?$"), frozenset({"GET", "HEAD"})),
]

#: Routes of the users plugin itself — they authenticate in their own
#: dependencies (with CSRF and password-change gating).
SELF_PROTECTED = re.compile(r"^/api/v1/qcli/(sessions|users|groups|tokens|link|permissions)(/.*)?$")

#: HTTP permission rules, first match wins: (path, methods or None, permission).
HTTP_RULES: list[Rule] = [
    (re.compile(r"^/api/(v1/)?qcli/fs/(ls|cat|stat|download)/?$"), None, "files:read"),
    (re.compile(r"^/api/(v1/)?qcli/fs(/.*)?$"), None, "files:write"),
    (re.compile(r"^/api/(v1/)?qcli/data-explorer(/.*)?$"), None, "data:query"),
    (re.compile(r"^/api/(v1/)?qcli/jobs(/.*)?$"), frozenset({"GET", "HEAD"}), "jobs:read"),
    (re.compile(r"^/api/(v1/)?qcli/jobs(/.*)?$"), None, "jobs:manage"),
    # Audit plugin (the users plugin mounts it).
    (re.compile(r"^/api/v1/qcli/audit/?$"), frozenset({"GET", "HEAD"}), "audit:read"),
    (re.compile(r"^/api/v1/qcli/audit/?$"), None, "audit:clear"),
    (re.compile(r"^/api/(v1/)?qcli/execute(/stream)?/?$"), None, "commands:execute"),
    # MCP endpoint (tools run commands; each tool also needs the processor's
    # required permissions).
    (re.compile(r"^/api/qcli/mcp/?$"), None, "commands:execute"),
]

WS_RULES: list[tuple[re.Pattern[str], str | None]] = [
    (re.compile(r"^/ws/(v1/)?qcli/events/?$"), None),
    (re.compile(r"^/ws/(v1/)?qcli/logs/?$"), "logs:read"),
    (re.compile(r"^/ws/(v1/)?qcli/shell/?$"), "shell:execute"),
]


#: The CLI's own HTTP and WebSocket paths (``host`` API tokens are refused
#: there).  The middleware protects every ``/api/`` and ``/ws/`` path, the
#: host application's routes included, which accept host tokens.
CLI_PATH = re.compile(r"^/(api|ws)/(v\d+/)?(qcli|cli)(/|$)")


def is_cli_path(path: str, base_paths: list[str] | None = None) -> bool:
    """A CLI route: ``/api|ws/[vN/]qcli|cli/...`` or under a custom CLI base
    path."""
    if CLI_PATH.match(path):
        return True
    return any(path == base or path.startswith(base + "/") for base in base_paths or [])


#: The longest WebSocket close reason (bytes of UTF-8) a close frame carries.
MAX_CLOSE_REASON_BYTES = 123


def close_reason(text: str) -> str:
    """``text`` as a WebSocket close reason, ``forbidden`` when too long."""
    return text if len(text.encode("utf-8")) <= MAX_CLOSE_REASON_BYTES else "forbidden"


def http_permission(path: str, method: str, base_paths: list[str] | None = None) -> str | None:
    for pattern, methods, permission in HTTP_RULES:
        if pattern.match(path) and (methods is None or method in methods):
            return permission
    for base in base_paths or []:
        if re.match(rf"^{re.escape(base)}/execute(/stream)?/?$", path):
            return "commands:execute"
    return None


def _normalize_base(path: str) -> str:
    return "/" + path.strip().strip("/")


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
        base_paths: list[str] | None = None,
        cli_prefixes: Callable[[], list[str] | None] | None = None,
    ) -> None:
        self.app = app
        self.auth = auth_service
        self.ensure_ready = ensure_ready
        self.extra_public = extra_public if extra_public is not None else []
        #: Custom CLI API base paths (``CliServerOptions.base_path``) — also
        #: protected, wherever they are mounted.
        self.base_paths = base_paths if base_paths is not None else []
        #: The CLI's route prefixes as the server knows them
        #: (``CliServerResult.cli_route_prefixes``: the core's, and every
        #: mounted plugin's API and dashboard prefix), read per request.
        self.cli_prefixes = cli_prefixes

    # ------------------------------------------------------------------

    def _is_public(self, path: str, method: str) -> bool:
        for pattern, methods in PUBLIC_PATHS:
            if pattern.match(path) and (methods is None or method in methods):
                return True
        for public_path, methods in self.extra_public:
            if path.rstrip("/") == public_path.rstrip("/") and (methods is None or method in methods):
                return True
        if method in ("GET", "HEAD"):
            for base in self.base_paths:
                if path.rstrip("/") in (base + "/version", base + "/capabilities"):
                    return True
        return False

    def _is_cli(self, path: str) -> bool:
        prefixes = list(self.base_paths)
        if self.cli_prefixes is not None:
            prefixes += list(self.cli_prefixes() or [])
        return is_cli_path(path, prefixes)

    async def _host_bearer(self, scope: dict) -> bool:
        """Whether the request carries a valid ``host`` API token as its
        bearer (refused on CLI paths the middleware does not authenticate:
        the admin dashboard's files, sockets without a ticket).  A look-up
        only: nothing is written (``last_used_at`` stays as it is)."""
        token = bearer_token(Headers(scope=scope))
        if not token or not token.startswith(API_TOKEN_PREFIX) or not self.auth.api_tokens_enabled:
            return False
        record = await self.auth.api_token_store.get_token_by_hash(hash_token(token))
        return record is not None and record.is_host

    def _protected(self, path: str) -> bool:
        if path.startswith("/api/") or path.startswith("/ws/"):
            return True
        return any(path == base or path.startswith(base + "/") for base in self.base_paths)

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
        if method == "OPTIONS" or self._is_public(path, method) or SELF_PROTECTED.match(path):
            await self.app(scope, receive, send)
            return
        if not self._protected(path):
            # Not authenticated here (e.g. the admin dashboard's files), but a
            # host token is still refused on the CLI's routes.
            if self._is_cli(path) and await self._host_bearer(scope):
                await _send_json(send, 403, {"error": HOST_TOKENS_CANNOT_USE_CLI_ROUTES})
                return
            await self.app(scope, receive, send)
            return
        if self.ensure_ready is not None:
            await self.ensure_ready()
        try:
            ctx = await authenticate_request(self.auth, Request(scope))
            if ctx.is_host_token and self._is_cli(path):
                raise UsersApiError(403, HOST_TOKENS_CANNOT_USE_CLI_ROUTES)
            if ctx.must_change_password:
                raise UsersApiError(403, PASSWORD_CHANGE_REQUIRED)
            permission = http_permission(path, method, self.base_paths)
            if permission and not ctx.can(permission):
                raise UsersApiError(403, f"missing permission: {permission}")
        except UsersApiError as exc:
            await _send_json(send, exc.status_code, {"error": exc.message})
            return
        except HTTPException as exc:
            # Raised by an external auth adapter: keep its status and detail.
            await _send_json(send, exc.status_code, {"error": exc.detail})
            return
        set_scope_auth(scope, ctx)
        await self.app(scope, receive, send)

    async def _websocket(self, scope: dict, receive: Any, send: Any) -> None:
        path: str = scope["path"]
        if not self._protected(path):
            if self._is_cli(path) and await self._host_bearer(scope):
                await _close(receive, send, 4403, close_reason(HOST_TOKENS_CANNOT_USE_CLI_ROUTES))
                return
            await self.app(scope, receive, send)
            return
        if self.ensure_ready is not None:
            await self.ensure_ready()
        ticket = _query_param(scope, "ticket")
        ctx = await self.auth.consume_ticket(ticket)
        if ctx is None:
            # Sockets take tickets only; a host token offered as a bearer is
            # told why it is refused.
            if await self._host_bearer(scope):
                await _close(receive, send, 4403, close_reason(HOST_TOKENS_CANNOT_USE_CLI_ROUTES))
            else:
                await _close(receive, send, 4401, "unauthorized")
            return
        # The users plugin's own refusals carry their text; a missing
        # permission keeps "forbidden".
        if ctx.is_host_token:
            await _close(receive, send, 4403, close_reason(HOST_TOKENS_CANNOT_USE_CLI_ROUTES))
            return
        if ctx.must_change_password:
            await _close(receive, send, 4403, close_reason(PASSWORD_CHANGE_REQUIRED))
            return
        permission = ws_permission(path)
        if permission and not ctx.can(permission):
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


__all__ = [
    "AuthContext", "ServerProtectionMiddleware", "close_reason", "http_permission", "is_cli_path",
    "ws_permission", "SAFE_METHODS",
]
