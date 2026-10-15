"""Fluent builder for configuring and creating the users plugin.

Usage::

    users_plugin = (
        CliUsersBuilder()
        .set_default_admin_password("admin")
        .set_session_timeout(30 * 60 * 1000)
        .build()
    )
    # Protects every server route/socket and scopes user events to the
    # user's own event sockets.
    result.mount_plugin(users_plugin)

    # in the app lifespan / startup hook (running event loop):
    await users_plugin.startup()    # seed + start session cleanup
    ...
    await users_plugin.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter

from .controllers.audit_controller import create_audit_router
from .controllers.groups_controller import create_groups_router
from .controllers.sessions_controller import create_sessions_router
from .controllers.users_controller import create_users_router
from .middleware.auth_middleware import create_auth_dependencies
from .models import ExternalAuthAdapter
from .server_protection import ServerProtectionMiddleware
from .shell_access import UsersShellAccess
from .services.auth_service import (
    DEFAULT_LOCKOUT_MS,
    DEFAULT_MAX_LOGIN_ATTEMPTS,
    DEFAULT_MAX_SESSION_LIFETIME_MS,
    DEFAULT_MIN_PASSWORD_LENGTH,
    DEFAULT_RATE_LIMIT_ATTEMPTS,
    DEFAULT_RATE_LIMIT_WINDOW_MS,
    DEFAULT_SESSION_TIMEOUT_MS,
    AuthService,
    AuthServiceOptions,
    BroadcastFn,
)
from .services.users_service import UsersService
from .stores.in_memory_audit_store import InMemoryAuditStore
from .stores.in_memory_credential_store import InMemoryCredentialStore
from .stores.in_memory_group_store import InMemoryGroupStore
from .stores.in_memory_session_store import InMemorySessionStore
from .stores.in_memory_user_store import InMemoryUserStore
from .stores.interfaces import (
    IAuditStore,
    ICredentialStore,
    IGroupStore,
    ISessionStore,
    IUserStore,
)

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/qcli"
DEFAULT_CLEANUP_INTERVAL_MS = 5 * 60 * 1000


@dataclass
class CliUsersPlugin:
    """The built users plugin.

    ``prefix`` / ``router`` make it mountable via
    ``CliServerResult.mount_plugin``.  Call :meth:`startup` from the app's
    lifespan (or startup hook) and :meth:`shutdown` on exit.
    """

    prefix: str
    router: APIRouter
    auth_service: AuthService
    users_service: UsersService
    user_store: IUserStore
    session_store: ISessionStore
    credential_store: ICredentialStore
    group_store: IGroupStore
    audit_store: IAuditStore
    default_admin_password: Optional[str] = None
    cleanup_interval_ms: int = DEFAULT_CLEANUP_INTERVAL_MS
    #: Protect every CLI server route/socket when mounted (B1).
    protect_server: bool = True
    #: FastAPI dependency: authenticated caller (``AuthContext``) — for
    #: host-app routes.  401 / 403 (password change required).
    require_auth: Any = None
    #: ``require_permission("perm")`` → FastAPI dependency (403 if missing).
    require_permission: Any = None
    _extra_public: list = field(default_factory=list, repr=False)
    _protected_apps: list = field(default_factory=list, repr=False)
    _cleanup_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _seeded: bool = field(default=False, repr=False)
    _seed_lock: Optional[asyncio.Lock] = field(default=None, repr=False)

    async def ensure_seeded(self) -> None:
        """Seed built-in groups and the root user once (idempotent)."""
        if self._seeded:
            return
        if self._seed_lock is None:
            self._seed_lock = asyncio.Lock()
        async with self._seed_lock:
            if self._seeded:
                return
            await self.auth_service.seed(self.default_admin_password)
            self._seeded = True

    @property
    def auth(self) -> AuthService:
        return self.auth_service

    def allow_anonymous(self, path: str, methods: list[str] | None = None) -> None:
        """Exempt an exact path from server protection (e.g. another plugin's
        login endpoint)."""
        self._extra_public.append(
            (path, frozenset(m.upper() for m in methods) if methods else None),
        )

    def protect(self, app: Any) -> None:
        """Install the server-protection middleware on ``app`` (innermost,
        so CORS stays outermost).  Must be called before the app starts."""
        if any(a is app for a in self._protected_apps):
            return
        from starlette.middleware import Middleware

        app.user_middleware.append(Middleware(
            ServerProtectionMiddleware,
            auth_service=self.auth_service,
            ensure_ready=self.ensure_seeded,
            extra_public=self._extra_public,
        ))
        app.middleware_stack = None
        self._protected_apps.append(app)

    def on_mount(self, server: Any) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: scopes user events
        to the user's own event sockets, adopts the server's allowed origins
        (CSRF), protects the whole server unless disabled and owns access to
        the remote shell (``users`` shell mode)."""
        manager = getattr(server, "event_socket_manager", None)
        if manager is not None and hasattr(manager, "send_to_user"):
            self.auth_service.user_event_sender = manager.send_to_user
        options = getattr(server, "options", None)
        origins = getattr(options, "allowed_origins", None)
        if origins and self.auth_service.options.allowed_origins is None:
            self.auth_service.options.allowed_origins = list(origins)
        if self.protect_server:
            self.protect(server.app)
        # Remote shell: users mode (ticket + shell:execute, session-bound).
        set_shell_provider = getattr(server, "set_shell_access_provider", None)
        if callable(set_shell_provider):
            set_shell_provider(UsersShellAccess(self.auth_service))

    async def startup(self) -> None:
        """Seed and start the background session sweep on the running loop."""
        await self.ensure_seeded()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.get_running_loop().create_task(self._cleanup_loop())

    async def shutdown(self) -> None:
        """Stop the background session sweep."""
        task, self._cleanup_task = self._cleanup_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, RuntimeError):
            pass

    def dispose(self) -> None:
        """Synchronous best-effort stop (prefer :meth:`shutdown`)."""
        task, self._cleanup_task = self._cleanup_task, None
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                pass  # loop already closed

    async def _cleanup_loop(self) -> None:
        interval = max(self.cleanup_interval_ms, 1) / 1000
        while True:
            await asyncio.sleep(interval)
            try:
                await self.auth_service.cleanup_sessions()
            except Exception:
                logger.exception("Session cleanup error")


class CliUsersBuilder:
    """Fluent builder for the users plugin."""

    def __init__(self) -> None:
        self._user_store: IUserStore | None = None
        self._session_store: ISessionStore | None = None
        self._credential_store: ICredentialStore | None = None
        self._audit_store: IAuditStore | None = None
        self._group_store: IGroupStore | None = None
        self._auth_adapter: ExternalAuthAdapter | None = None
        self._default_admin_password: str | None = None
        self._session_timeout_ms = DEFAULT_SESSION_TIMEOUT_MS
        self._max_session_lifetime_ms = DEFAULT_MAX_SESSION_LIFETIME_MS
        self._rate_limit_attempts = DEFAULT_RATE_LIMIT_ATTEMPTS
        self._rate_limit_window_ms = DEFAULT_RATE_LIMIT_WINDOW_MS
        self._trust_proxy = False
        self._allowed_origins: list[str] | None = None
        self._protect_server = True
        self._min_password_length = DEFAULT_MIN_PASSWORD_LENGTH
        self._max_login_attempts = DEFAULT_MAX_LOGIN_ATTEMPTS
        self._lockout_ms = DEFAULT_LOCKOUT_MS
        self._cleanup_interval_ms = DEFAULT_CLEANUP_INTERVAL_MS

    def set_user_store(self, store: IUserStore) -> CliUsersBuilder:
        self._user_store = store
        return self

    def set_session_store(self, store: ISessionStore) -> CliUsersBuilder:
        self._session_store = store
        return self

    def set_credential_store(self, store: ICredentialStore) -> CliUsersBuilder:
        self._credential_store = store
        return self

    def set_audit_store(self, store: IAuditStore) -> CliUsersBuilder:
        self._audit_store = store
        return self

    def set_group_store(self, store: IGroupStore) -> CliUsersBuilder:
        self._group_store = store
        return self

    def set_auth_adapter(self, adapter: ExternalAuthAdapter | None) -> CliUsersBuilder:
        """Delegate authentication to the host app (see README)."""
        self._auth_adapter = adapter
        return self

    def set_default_admin_password(self, password: str | None) -> CliUsersBuilder:
        """Password of the seeded ``root`` user.  When not set, a random
        password is generated, logged once, and must be changed at first
        login."""
        self._default_admin_password = password
        return self

    def set_max_session_lifetime(self, lifetime_ms: int) -> CliUsersBuilder:
        """Absolute session lifetime (default 12 hours)."""
        self._max_session_lifetime_ms = lifetime_ms
        return self

    def set_rate_limit(self, attempts: int, window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS) -> CliUsersBuilder:
        """Per-IP limit for login / verify-password / self password change
        (default 20 per minute)."""
        self._rate_limit_attempts = attempts
        self._rate_limit_window_ms = window_ms
        return self

    def set_trust_proxy(self, trust: bool = True) -> CliUsersBuilder:
        """Use ``X-Forwarded-For`` for the client IP (only behind a proxy)."""
        self._trust_proxy = trust
        return self

    def set_allowed_origins(self, origins: list[str] | None) -> CliUsersBuilder:
        """Origins accepted for bearer-less state-changing requests (CSRF,
        adapter mode).  Defaults to the server's ``allowed_origins``."""
        self._allowed_origins = list(origins) if origins is not None else None
        return self

    def set_protect_server(self, protect: bool) -> CliUsersBuilder:
        """Protect every CLI server route and socket (default ``True``)."""
        self._protect_server = protect
        return self

    def set_session_timeout(self, timeout_ms: int) -> CliUsersBuilder:
        """Idle session timeout in milliseconds (default 30 minutes)."""
        self._session_timeout_ms = timeout_ms
        return self

    def set_min_password_length(self, length: int) -> CliUsersBuilder:
        self._min_password_length = length
        return self

    def set_max_login_attempts(self, attempts: int) -> CliUsersBuilder:
        self._max_login_attempts = attempts
        return self

    def set_lockout(self, lockout_ms: int) -> CliUsersBuilder:
        """Lockout duration after too many failed logins (default 5 minutes)."""
        self._lockout_ms = lockout_ms
        return self

    def set_cleanup_interval(self, interval_ms: int) -> CliUsersBuilder:
        """Interval of the background expired-session sweep (default 5 min)."""
        self._cleanup_interval_ms = interval_ms
        return self

    def build(self, broadcast_fn: BroadcastFn | None = None) -> CliUsersPlugin:
        """Build the plugin.  ``broadcast_fn`` receives JSON-encoded events
        (e.g. ``session-kicked``); it may be sync or async."""
        auth_service = AuthService(
            self._user_store or InMemoryUserStore(),
            self._session_store or InMemorySessionStore(),
            self._credential_store or InMemoryCredentialStore(),
            self._audit_store or InMemoryAuditStore(),
            self._group_store or InMemoryGroupStore(),
            options=AuthServiceOptions(
                session_timeout_ms=self._session_timeout_ms,
                max_session_lifetime_ms=self._max_session_lifetime_ms,
                min_password_length=self._min_password_length,
                max_login_attempts=self._max_login_attempts,
                lockout_ms=self._lockout_ms,
                rate_limit_attempts=self._rate_limit_attempts,
                rate_limit_window_ms=self._rate_limit_window_ms,
                trust_proxy=self._trust_proxy,
                allowed_origins=self._allowed_origins,
            ),
            broadcast_fn=broadcast_fn,
            auth_adapter=self._auth_adapter,
        )
        users_service = UsersService(auth_service)

        router = APIRouter()
        plugin = CliUsersPlugin(
            prefix=PREFIX,
            router=router,
            auth_service=auth_service,
            users_service=users_service,
            user_store=auth_service.user_store,
            session_store=auth_service.session_store,
            credential_store=auth_service.credential_store,
            group_store=auth_service.group_store,
            audit_store=auth_service.audit_store,
            default_admin_password=self._default_admin_password,
            cleanup_interval_ms=self._cleanup_interval_ms,
            protect_server=self._protect_server,
        )

        # Requests made before startup() (e.g. apps without a lifespan) still
        # see the seeded built-ins.
        get_auth, require_permission, get_auth_pending = create_auth_dependencies(
            auth_service, ensure_ready=plugin.ensure_seeded,
        )
        plugin.require_auth = get_auth
        plugin.require_permission = require_permission
        router.include_router(create_sessions_router(
            auth_service, get_auth, require_permission,
            ensure_ready=plugin.ensure_seeded, get_auth_pending=get_auth_pending,
        ))
        router.include_router(create_users_router(
            auth_service, users_service, get_auth, require_permission,
            get_auth_pending=get_auth_pending,
        ))
        router.include_router(create_groups_router(
            auth_service, users_service, get_auth, require_permission,
        ))
        router.include_router(create_audit_router(auth_service, require_permission))
        return plugin
