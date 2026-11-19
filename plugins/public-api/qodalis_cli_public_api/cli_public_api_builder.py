"""Fluent builder for the public-api plugin.

Usage::

    users_plugin = CliUsersBuilder().build()
    public_api = (
        CliPublicApiBuilder()
        .set_users(users_plugin)
        .set_quota(lambda caller: PublicApiQuota(requests_per_day=1000, requests_per_minute=60))
        .build()
    )
    result.mount_plugin(users_plugin)
    result.mount_plugin(public_api)

    @result.app.get("/v1/rates")
    async def rates(caller: QcliCaller = Depends(public_api.guard("rates:read"))):
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from qodalis_cli_users import QcliCaller, QcliGuardError
from qodalis_cli_users.errors import install_guard_error_handler
from qodalis_cli_users.middleware.auth_middleware import authenticate_host_token, tag_guard

from .headers_middleware import STATE_KEY, RateLimitHeadersMiddleware
from .openapi import PublicOpenApiDocument, check_source
from .swagger_ui import ASSETS as SWAGGER_ASSETS
from .swagger_ui import ASSETS_DIR as SWAGGER_ASSETS_DIR
from .swagger_ui import is_root_relative, swagger_html
from .quota import PublicApiQuota, QuotaResolver
from .usage_service import (
    PublicApiUsageService,
    day_window,
    minute_window,
    resolve_quota,
    utc_now,
)
from .usage_store import InMemoryPublicApiUsageStore, IPublicApiUsageStore

logger = logging.getLogger(__name__)

DEFAULT_BASE_PATH = "/qcli/api"
DEFAULT_TITLE = "API reference"
DEFAULT_RETENTION_DAYS = 31
#: Minute windows are kept this long (the current one and the one before).
MINUTE_RETENTION = timedelta(minutes=2)
PRUNE_INTERVAL = timedelta(hours=1)

INTERNAL_ERROR = "internal server error"
NO_API_ACCESS = "forbidden: no API access"
USERS_REQUIRED = "public-api: set_users(users_plugin) is required"
#: ``request.state`` flag: the request was admitted (counted) by a guard.
COUNTED_KEY = "qcli_public_api_counted"
SWAGGER_ASSETS_TEXT = "public-api: Swagger UI assets must be root-relative paths"
NOT_FOUND = "not found"


def validate_base_path(path: Any, label: str) -> str:
    """A mount path: starts with ``/``, no trailing ``/``, not ``/`` itself."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or path.endswith("/")
        or any(c.isspace() or ord(c) < 0x20 or c in "?#\\" for c in path)
        or "//" in path
    ):
        raise ValueError(f"invalid {label} base path: {path}")
    return path


INVALID_PATH_BASE = "invalid path base"


def path_base(request: Request) -> str | None:
    """The ASGI root path (no trailing ``/``), or ``None`` when it would make
    a URL leave the host (it starts with two slashes or a slash and a
    backslash)."""
    root = request.scope.get("root_path", "") or ""
    if root.startswith("//") or root.startswith("/\\"):
        return None
    return root.rstrip("/")


def invalid_path_base() -> Response:
    return JSONResponse({"error": INVALID_PATH_BASE}, status_code=400)


def _drop_caller(request: Request) -> None:
    """A later stacked guard refused: the request has no caller."""
    state = request.scope.get("state")
    if isinstance(state, dict):
        state.pop("qcli_caller", None)


def _stacked_permissions(request: Request) -> list[str]:
    """Permissions of every tagged guard of the matched route (public-api
    guards and the users plugin's ``require_permission`` /
    ``require_host_token``; FastAPI puts the route in ``scope["route"]``),
    so the first public-api guard to run refuses a request another guard
    would refuse before counting it.  Empty when the route is not known."""
    route = request.scope.get("route")
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", None) or [])
    found: list[str] = []
    while stack:
        dep = stack.pop(0)
        call = getattr(dep, "call", None)
        if hasattr(call, "qcli_host_token_only"):
            permission = getattr(call, "qcli_permission", None)
            if permission and permission not in found:
                found.append(permission)
        stack.extend(getattr(dep, "dependencies", None) or [])
    return found


def _seconds_until(now: datetime, then: datetime) -> int:
    return max(1, math.ceil((then - now).total_seconds()))


def seconds_to_next_day(now: datetime) -> int:
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return _seconds_until(now, midnight)


def seconds_to_next_minute(now: datetime) -> int:
    return _seconds_until(now, now.replace(second=0, microsecond=0) + timedelta(minutes=1))


@dataclass
class CliPublicApiPlugin:
    """The built public-api plugin: mount it with
    ``CliServerResult.mount_plugin`` (after the users plugin), then guard
    the host's public routes with :meth:`guard`."""

    prefix: str
    router: APIRouter
    #: Usage of tokens and accounts (``await usage.account(user_id)``).
    usage: PublicApiUsageService
    #: What the portal plugin shows (``CliPortalBuilder().add_section(...)``).
    portal_section: dict[str, str]
    store: IPublicApiUsageStore
    users: Any
    #: The public OpenAPI document (``<prefix>/openapi.json``).
    document: PublicOpenApiDocument
    title: str = DEFAULT_TITLE
    #: Host routes: never counted among the CLI's routes.
    cli_routes: bool = False
    #: The document and Swagger UI are served without sign-in, also under
    #: ``/api/`` on a protected server (the guarded routes are elsewhere).
    public_paths: list[str] = field(default_factory=list)
    quota_resolver: Optional[QuotaResolver] = None
    retention_days: int = DEFAULT_RETENTION_DAYS
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    _last_prune: Optional[datetime] = field(default=None, repr=False)
    _account_locks: Any = field(default_factory=weakref.WeakValueDictionary, repr=False)

    def guard(self, permission: str | None = None) -> Any:
        """FastAPI dependency for a public route: a ``host`` API token
        holding ``permission`` (when given), within the account's quota.
        Returns the :class:`QcliCaller` (also ``request.state.qcli_caller``).

        Refusals, in order: the users plugin's authentication texts (401 /
        403), 403 ``forbidden: a host token is required``, 403
        ``forbidden: missing permission <permission>``, 500 ``internal
        server error`` (the quota resolver failed), 403 ``forbidden: no API
        access`` (a limit of 0), 429 per day, then per minute.  Nothing is
        counted for a refused request."""
        users = self.users
        # The caller is exposed only once the guard fully passed.
        authenticate = getattr(users, "authenticate", None) or users.require_auth

        async def dependency(request: Request) -> QcliCaller:
            try:
                return await check(request)
            except QcliGuardError:
                # A refused request has no caller, also when a users guard
                # that ran first on the route had set one.
                _drop_caller(request)
                raise

        async def check(request: Request) -> QcliCaller:
            ctx = await authenticate_host_token(authenticate, request)
            # Stacked guards (a router's and a route's): the first one checks
            # every stacked guard's permission, then counts the request once;
            # the others check only their own permission.
            counted = getattr(request.state, COUNTED_KEY, False)
            required = [permission] if counted else [permission, *_stacked_permissions(request)]
            for needed in required:
                if needed and not ctx.can(needed):
                    if counted:
                        setattr(request.state, STATE_KEY, None)
                    raise QcliGuardError(403, f"forbidden: missing permission {needed}")
            caller = ctx.caller
            if counted:
                return caller
            headers = await self._admit(caller)
            setattr(request.state, COUNTED_KEY, True)
            if headers:
                setattr(request.state, STATE_KEY, headers)
            request.state.qcli_caller = caller
            return caller

        dependency.qcli_public_api = True
        return tag_guard(dependency, permission, True)

    def _account_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._account_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[user_id] = lock
        return lock

    async def _admit(self, caller: QcliCaller) -> dict[str, str]:
        """Count the request or refuse it (C5 rows 4-7); returns the
        ``X-RateLimit-*`` headers of an admitted request."""
        try:
            quota = await resolve_quota(self.quota_resolver, caller)
        except Exception:
            logger.exception("public-api: the quota resolver failed")
            raise QcliGuardError(500, INTERNAL_ERROR) from None
        per_day, per_minute = quota.requests_per_day, quota.requests_per_minute
        if (per_day is not None and per_day <= 0) or (per_minute is not None and per_minute <= 0):
            raise QcliGuardError(403, NO_API_ACCESS)

        now = utc_now(self.clock)
        day, minute = day_window(now), minute_window(now)
        token_id = caller.token_id or ""
        store = self.store
        #: Windows incremented so far (taken back when the store fails).
        incremented: list[str] = []
        try:
            # One admission at a time per account in this process: exactly the
            # limit here; across processes the compensation keeps the count at
            # or under it.
            lock = self._account_lock(caller.user_id)
            async with lock:
                for window in (day, minute):
                    await store.increment(token_id, caller.user_id, window)
                    incremented.append(window)
                day_count = await store.account_count(caller.user_id, day) if per_day is not None else 0
                minute_count = await store.account_count(caller.user_id, minute) if per_minute is not None else 0
                exceeded = None
                if per_day is not None and day_count > per_day:
                    exceeded = "day"
                elif per_minute is not None and minute_count > per_minute:
                    exceeded = "minute"
                if exceeded is not None:
                    while incremented:
                        await store.decrement(token_id, incremented[-1])
                        incremented.pop()
            # Inside the handling below: a cancellation while pruning takes
            # the counts back too.
            await self._maybe_prune(now)
        except anyio.get_cancelled_exc_class():
            # The request was cancelled (the client went away): not counted.
            # Shielded, so the take-back's own awaits are not cancelled.
            with anyio.CancelScope(shield=True):
                await self._take_back(token_id, incremented)
            raise
        except Exception:
            logger.exception("public-api: the usage store failed")
            # The request is not served: take back what was counted (best effort).
            await self._take_back(token_id, incremented)
            raise QcliGuardError(500, INTERNAL_ERROR) from None

        headers: dict[str, str] = {}
        if per_day is not None:
            limit, count, reset = per_day, day_count, seconds_to_next_day(now)
        elif per_minute is not None:
            limit, count, reset = per_minute, minute_count, seconds_to_next_minute(now)
        else:
            limit = None
        if limit is not None:
            headers = {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(0 if exceeded else max(0, limit - count)),
                "X-RateLimit-Reset": str(reset),
            }
        if exceeded == "day":
            headers["Retry-After"] = str(seconds_to_next_day(now))
            raise QcliGuardError(429, f"quota exceeded: {per_day} requests per day", headers)
        if exceeded == "minute":
            headers["Retry-After"] = str(seconds_to_next_minute(now))
            raise QcliGuardError(429, f"too many requests: {per_minute} per minute", headers)
        return headers

    async def _take_back(self, token_id: str, windows: list[str]) -> None:
        """Decrement every window, also when the task is cancelled (natively)
        meanwhile: an interrupted decrement is retried (at most three times)
        and the cancellation is raised again once all are done."""
        cancelled: BaseException | None = None
        for window in reversed(windows):
            for _ in range(3):
                try:
                    await self.store.decrement(token_id, window)
                    break
                except Exception:
                    logger.exception("public-api: taking back a request count failed")
                    break
                except BaseException as exc:  # cancellation: finish first
                    cancelled = exc
        windows.clear()
        if cancelled is not None:
            raise cancelled

    async def _maybe_prune(self, now: datetime) -> None:
        if self._last_prune is not None and now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        before_day = (now - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        before_minute = minute_window(now - MINUTE_RETENTION)
        try:
            await self.store.prune(before_day, before_minute)
        except Exception:
            logger.exception("public-api: pruning the usage store failed")

    def on_mount(self, server: Any) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: the token views
        get ``usage``, refusals answer ``{"error": …}`` and admitted
        requests carry their ``X-RateLimit-*`` headers."""
        self.users.set_token_usage_provider(self.usage.tokens)
        app = server.app
        install_guard_error_handler(app)
        if not any(getattr(m, "cls", None) is RateLimitHeadersMiddleware for m in app.user_middleware):
            app.add_middleware(RateLimitHeadersMiddleware)


class CliPublicApiBuilder:
    """Fluent builder for the public-api plugin."""

    def __init__(self) -> None:
        self._users: Any = None
        self._base_path: Any = DEFAULT_BASE_PATH
        self._title = DEFAULT_TITLE
        self._title_set = False
        self._server_url: str | None = None
        self._openapi: Any = None
        self._quota: QuotaResolver | None = None
        self._store: IPublicApiUsageStore | None = None
        self._retention_days = DEFAULT_RETENTION_DAYS
        self._clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._version: str | None = None
        #: ``None``: the vendored files under ``<base>/swagger/``.
        self._swagger_js_url: str | None = None
        self._swagger_css_url: str | None = None

    def set_users(self, users_plugin: Any) -> CliPublicApiBuilder:
        """The users plugin whose tokens the guard accepts (required)."""
        self._users = users_plugin
        return self

    def set_base_path(self, path: str) -> CliPublicApiBuilder:
        """Where the OpenAPI document and Swagger UI are served (default
        ``/qcli/api``): starts with ``/``, no trailing ``/``, not ``/``."""
        self._base_path = path
        return self

    def set_title(self, title: str) -> CliPublicApiBuilder:
        """The portal's label of the API section (default ``API reference``).
        A document read from an app or a router also takes it as
        ``info.title`` (default ``API``); a ``dict`` keeps its own."""
        self._title = title
        self._title_set = True
        return self

    def set_version(self, version: str) -> CliPublicApiBuilder:
        """``info.version`` of a document read from an app or a router
        (default ``1.0.0``)."""
        self._version = version
        return self

    def set_swagger_assets(self, js_url: str, css_url: str) -> CliPublicApiBuilder:
        """Swagger UI's script and stylesheet, served by the host itself
        (default: the copies vendored in this package, under
        ``<base>/swagger/``).  Root-relative paths only: the page never loads
        anything from another origin."""
        if not (is_root_relative(js_url) and is_root_relative(css_url)):
            raise ValueError(SWAGGER_ASSETS_TEXT)
        self._swagger_js_url = js_url
        self._swagger_css_url = css_url
        return self

    def set_server_url(self, url: str | None) -> CliPublicApiBuilder:
        """The document's ``servers`` URL (default: the request's
        ``scheme://host`` and root path)."""
        self._server_url = url
        return self

    def set_openapi(self, source: Any) -> CliPublicApiBuilder:
        """The public OpenAPI document: a ``dict``, a zero-argument callable
        returning one (called per request), a ``FastAPI`` app or an
        ``APIRouter`` (its routes, read on the first request; routes with
        ``include_in_schema=False`` are left out)."""
        self._openapi = check_source(source)
        return self

    def set_quota(self, resolver: QuotaResolver | None) -> CliPublicApiBuilder:
        """``resolver(caller)`` returns the account's
        :class:`PublicApiQuota` (sync or async); without one the API is
        unlimited."""
        if resolver is not None and not callable(resolver):
            raise TypeError("quota resolver must be callable")
        self._quota = resolver
        return self

    def set_usage_store(self, store: IPublicApiUsageStore) -> CliPublicApiBuilder:
        """Where the counters live (default in memory, one process)."""
        self._store = store
        return self

    def set_usage_retention_days(self, days: int) -> CliPublicApiBuilder:
        """Days the per-day counters are kept (default 31)."""
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ValueError("usage retention days must be a positive integer")
        self._retention_days = days
        return self

    def set_clock(self, now: Callable[[], datetime]) -> CliPublicApiBuilder:
        """The clock (tests); read in UTC."""
        self._clock = now
        return self

    def build(self) -> CliPublicApiPlugin:
        if self._users is None:
            raise ValueError(USERS_REQUIRED)
        base_path = validate_base_path(self._base_path, "public-api")
        store = self._store or InMemoryPublicApiUsageStore()
        holder: dict[str, CliPublicApiPlugin] = {}
        usage = PublicApiUsageService(
            self._users,
            store=lambda: holder["plugin"].store,
            resolver=lambda: holder["plugin"].quota_resolver,
            clock=lambda: holder["plugin"].clock(),
        )
        plugin = CliPublicApiPlugin(
            prefix=base_path,
            router=APIRouter(),
            usage=usage,
            portal_section={"kind": "api", "title": self._title, "base_path": base_path},
            store=store,
            users=self._users,
            document=PublicOpenApiDocument(
                self._openapi,
                title=self._title if self._title_set else None,
                version=self._version,
                server_url=self._server_url,
            ),
            title=self._title,
            quota_resolver=self._quota,
            retention_days=self._retention_days,
            clock=self._clock,
        )
        holder["plugin"] = plugin
        plugin.router = self._router(plugin)
        plugin.public_paths = [
            f"{base_path}/openapi.json", f"{base_path}/swagger", f"{base_path}/swagger/", f"{base_path}/swagger/*",
        ]
        return plugin

    def _router(self, plugin: CliPublicApiPlugin) -> APIRouter:
        router = APIRouter()
        document = plugin.document
        js_url, css_url = self._swagger_js_url, self._swagger_css_url

        def base_url(request: Request) -> str:
            # Includes the ASGI root path (an app mounted under a prefix).
            return str(request.base_url).rstrip("/")

        @router.api_route("/openapi.json", methods=["GET", "HEAD"], include_in_schema=False)
        async def openapi_json(request: Request) -> Response:
            if path_base(request) is None:
                return invalid_path_base()
            body = json.dumps(document.build(base_url(request)), ensure_ascii=False)
            return Response(
                content=body.encode("utf-8"),
                media_type="application/json; charset=utf-8",
                headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
            )

        async def swagger(request: Request) -> Response:
            root = path_base(request)
            if root is None:
                return invalid_path_base()
            title = document.title_of(document.build(base_url(request)))
            own = f"{root}{plugin.prefix}/swagger/"
            # Every URL is same-origin and escaped in the page (the root path
            # comes from the server's configuration, but is never trusted).
            return HTMLResponse(swagger_html(
                title=title,
                openapi_url=f"{root}{plugin.prefix}/openapi.json",
                js_url=root + js_url if js_url else own + "swagger-ui-bundle.js",
                css_url=root + css_url if css_url else own + "swagger-ui.css",
                favicon_url=own + "favicon-32x32.png",
            ))

        async def swagger_asset(name: str) -> Response:
            # A fixed allow-list: nothing else of the package is reachable.
            media_type = SWAGGER_ASSETS.get(name)
            if media_type is None:
                return JSONResponse({"error": NOT_FOUND}, status_code=404)
            return FileResponse(
                os.path.join(SWAGGER_ASSETS_DIR, name), media_type=media_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )

        methods = ["GET", "HEAD"]
        router.add_api_route("/swagger", swagger, methods=methods, include_in_schema=False)
        router.add_api_route("/swagger/", swagger, methods=methods, include_in_schema=False)
        # ``path`` so a name with an encoded "/" still gets this 404.
        router.add_api_route("/swagger/{name:path}", swagger_asset, methods=methods, include_in_schema=False)
        return router
