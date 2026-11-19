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

import logging
import math
import weakref
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request

from qodalis_cli_users import QcliCaller, QcliGuardError
from qodalis_cli_users.errors import install_guard_error_handler
from qodalis_cli_users.middleware.auth_middleware import authenticate_host_token, tag_guard

from .headers_middleware import STATE_KEY, RateLimitHeadersMiddleware
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


def validate_base_path(path: Any, label: str) -> str:
    """A mount path: starts with ``/``, no trailing ``/``, not ``/`` itself."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or path.endswith("/")
        or any(c.isspace() or ord(c) < 0x20 for c in path)
        or "//" in path
    ):
        raise ValueError(f"invalid {label} base path: {path}")
    return path


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
    title: str = DEFAULT_TITLE
    #: Host routes: never counted among the CLI's routes.
    cli_routes: bool = False
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

        async def dependency(request: Request) -> QcliCaller:
            ctx = await authenticate_host_token(users.require_auth, request)
            if permission and not ctx.can(permission):
                raise QcliGuardError(403, f"forbidden: missing permission {permission}")
            caller = ctx.caller
            request.state.qcli_caller = caller
            headers = await self._admit(caller)
            if headers:
                setattr(request.state, STATE_KEY, headers)
            return caller

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
        try:
            # One admission at a time per account in this process: exactly the
            # limit here; across processes the compensation keeps the count at
            # or under it.
            lock = self._account_lock(caller.user_id)
            async with lock:
                await store.increment(token_id, caller.user_id, day)
                await store.increment(token_id, caller.user_id, minute)
                day_count = await store.account_count(caller.user_id, day) if per_day is not None else 0
                minute_count = await store.account_count(caller.user_id, minute) if per_minute is not None else 0
                exceeded = None
                if per_day is not None and day_count > per_day:
                    exceeded = "day"
                elif per_minute is not None and minute_count > per_minute:
                    exceeded = "minute"
                if exceeded is not None:
                    await store.decrement(token_id, day)
                    await store.decrement(token_id, minute)
        except Exception:
            logger.exception("public-api: the usage store failed")
            raise QcliGuardError(500, INTERNAL_ERROR) from None

        await self._maybe_prune(now)

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
        self._server_url: str | None = None
        self._openapi: Any = None
        self._quota: QuotaResolver | None = None
        self._store: IPublicApiUsageStore | None = None
        self._retention_days = DEFAULT_RETENTION_DAYS
        self._clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

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
        """The portal's label of the API section (default ``API reference``);
        the document's ``info.title`` stays its own."""
        self._title = title
        return self

    def set_server_url(self, url: str | None) -> CliPublicApiBuilder:
        """The document's ``servers`` URL (default: the request's
        ``scheme://host`` and root path)."""
        self._server_url = url
        return self

    def set_openapi(self, source: Any) -> CliPublicApiBuilder:
        """The public OpenAPI document: a ``dict``, a zero-argument callable
        returning one, a ``FastAPI`` app or an ``APIRouter``."""
        self._openapi = source
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
            title=self._title,
            quota_resolver=self._quota,
            retention_days=self._retention_days,
            clock=self._clock,
        )
        holder["plugin"] = plugin
        return plugin
