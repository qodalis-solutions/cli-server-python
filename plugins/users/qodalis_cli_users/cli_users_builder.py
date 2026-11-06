"""Fluent builder for configuring and creating the users plugin.

Usage::

    users_plugin = (
        CliUsersBuilder()
        .set_default_admin_password("admin")
        .set_session_timeout(30 * 60 * 1000)
        .build()
    )
    # Protects every server route/socket, scopes user events to the user's
    # own event sockets and mounts the audit plugin (its events' log).
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

from fastapi import APIRouter, Request

from qodalis_cli_audit import CliAuditBuilder, CliAuditPlugin
from qodalis_cli_server_abstractions.events import ICliEventBus

from .controllers.groups_controller import create_groups_router
from .controllers.sessions_controller import create_sessions_router
from .controllers.tokens_controller import create_tokens_router
from .controllers.users_controller import create_users_router
from .middleware.auth_middleware import create_auth_dependencies
from .errors import UsersApiError
from .models import ExternalAuthAdapter
from .server_protection import ServerProtectionMiddleware, _normalize_base
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
from .stores.in_memory_api_token_store import InMemoryApiTokenStore
from .stores.in_memory_credential_store import InMemoryCredentialStore
from .stores.in_memory_group_store import InMemoryGroupStore
from .stores.in_memory_session_store import InMemorySessionStore
from .stores.in_memory_user_store import InMemoryUserStore
from .stores.interfaces import (
    IApiTokenStore,
    IAuditStore,
    ICredentialStore,
    IGroupStore,
    ISessionStore,
    IUserStore,
)

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/qcli"
DEFAULT_CLEANUP_INTERVAL_MS = 5 * 60 * 1000
#: Re-check interval (seconds) of events/logs sockets opened through the
#: external auth adapter: the server's ``shell.session_check_interval``
#: (default 30), at most 60, as for shells.
DEFAULT_SESSION_CHECK_INTERVAL = 30.0
MAX_SESSION_CHECK_INTERVAL = 60.0
LOGS_READ = "logs:read"


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
    api_token_store: Optional[IApiTokenStore] = None
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
    _base_paths: list = field(default_factory=list, repr=False)
    _protected_apps: list = field(default_factory=list, repr=False)
    _cleanup_task: Optional[asyncio.Task] = field(default=None, repr=False)
    #: ``(socket manager, permission)`` of the server's events/logs sockets.
    _session_sockets: list = field(default_factory=list, repr=False)
    _session_check_interval: Optional[float] = field(default=None, repr=False)
    _external_check_task: Optional[asyncio.Task] = field(default=None, repr=False)
    #: The audit plugin the users plugin records its events in, mounted with
    #: it (or joining the audit plugin the host mounted, see :meth:`on_mount`).
    audit_plugin: Optional[CliAuditPlugin] = None
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
            base_paths=self._base_paths,
        ))
        app.middleware_stack = None
        self._protected_apps.append(app)

    def on_mount(self, server: Any) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: scopes user events
        to the user's own event sockets, closes events/logs sockets whose session
        ended, adopts the server's allowed origins (CSRF), protects the whole
        server unless disabled and owns access to the remote shell (``users``
        shell mode)."""
        manager = getattr(server, "event_socket_manager", None)
        if manager is not None and hasattr(manager, "send_to_user"):
            self.auth_service.user_event_sender = manager.send_to_user
        # user:*, group:*, token:* and session:* events go to the server's bus.
        bus = getattr(server, "event_bus", None)
        if self.auth_service.event_bus is None and bus is not None:
            self.auth_service.event_bus = bus
        options = getattr(server, "options", None)
        # The host application signs users in (read by the admin dashboard).
        if self.auth_service.external_auth and hasattr(options, "external_authentication"):
            options.external_authentication = True
        origins = getattr(options, "allowed_origins", None)
        if origins and self.auth_service.options.allowed_origins is None:
            self.auth_service.options.allowed_origins = list(origins)
        # A custom CLI API base path is protected like the default ones.
        base_path = getattr(options, "base_path", None)
        if isinstance(base_path, str) and base_path.strip("/"):
            base = _normalize_base(base_path)
            if base not in self._base_paths:
                self._base_paths.append(base)
        if self.protect_server:
            self.protect(server.app)
        # Events/logs sockets are bound to their session: closed (4401
        # "session ended") on logout, revoke, disable/delete and expiry.
        # Those of adapter identities are re-checked through the adapter
        # (see startup()).
        for name, permission in (("event_socket_manager", None), ("log_socket_manager", LOGS_READ)):
            sockets = getattr(server, name, None)
            if sockets is not None and hasattr(sockets, "set_session_check"):
                sockets.set_session_check(self.auth_service.is_context_active)
                self.auth_service.add_change_listener(sockets.request_recheck)
                if hasattr(sockets, "close_ended_sessions") and not any(
                    s is sockets for s, _ in self._session_sockets
                ):
                    self._session_sockets.append((sockets, permission))
        shell_options = getattr(options, "shell", None)
        interval = getattr(shell_options, "session_check_interval", None)
        if isinstance(interval, (int, float)):
            self._session_check_interval = float(interval)
        # Remote shell: users mode (ticket + shell:execute, session-bound).
        set_shell_provider = getattr(server, "set_shell_access_provider", None)
        if callable(set_shell_provider):
            set_shell_provider(UsersShellAccess(self.auth_service))
        self._mount_audit(server)

    def _mount_audit(self, server: Any) -> None:
        """Mount the audit plugin with the users plugin, so a host that only
        mounts the users plugin keeps its audit log.  When the host mounted an
        audit plugin of its own, this one joins it: one log, in the store set
        on either side (the default in-memory store otherwise).  Without server
        protection the users plugin still guards the audit API (``audit:read``
        / ``audit:clear``)."""
        audit = self.audit_plugin
        mount = getattr(server, "mount_plugin", None)
        if audit is None or not callable(mount):
            return
        if not self.protect_server and audit.authorize is None:
            audit.authorize = self.authorize_audit
        mount(audit)

    async def authorize_audit(self, request: Request, permission: str) -> Any:
        """The users plugin's guard of the audit API (a session or token with
        ``permission``), used when it does not protect the whole server."""
        ctx = await self.require_auth(request)
        if not ctx.can(permission):
            raise UsersApiError(403, f"missing permission: {permission}")
        return ctx

    @property
    def external_socket_check_interval(self) -> float:
        """Seconds between re-checks of events/logs sockets opened through the
        external auth adapter: the server's ``shell.session_check_interval``
        (default 30), capped at 60 like the shell re-check."""
        value = self._session_check_interval
        if not value or value <= 0:
            return DEFAULT_SESSION_CHECK_INTERVAL
        return min(value, MAX_SESSION_CHECK_INTERVAL)

    async def startup(self) -> None:
        """Seed and start the background tasks on the running loop: the
        session sweep and, with an external auth adapter, the re-check of
        the events/logs sockets opened through it."""
        await self.ensure_seeded()
        loop = asyncio.get_running_loop()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = loop.create_task(self._cleanup_loop())
        if self.auth_service.external_auth and (
            self._external_check_task is None or self._external_check_task.done()
        ):
            self._external_check_task = loop.create_task(self._external_socket_loop())

    async def shutdown(self) -> None:
        """Stop the background tasks."""
        tasks = [self._cleanup_task, self._external_check_task]
        self._cleanup_task = self._external_check_task = None
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RuntimeError):
                pass

    def dispose(self) -> None:
        """Synchronous best-effort stop (prefer :meth:`shutdown`)."""
        tasks = [self._cleanup_task, self._external_check_task]
        self._cleanup_task = self._external_check_task = None
        for task in tasks:
            if task is not None:
                try:
                    task.cancel()
                except RuntimeError:
                    pass  # loop already closed

    async def recheck_external_sockets(self) -> None:
        """Run the external auth adapter again for every events/logs socket
        opened through it (on the snapshot of the request it accepted) and
        close with 4401 ``session ended`` those whose user is gone, another
        user, disabled, or no longer holds the socket's permission
        (``logs:read`` for logs), or when the adapter raises.  Sockets with a
        stored session or API token are left alone."""
        checks = []
        for sockets, permission in self._session_sockets:
            async def check(ctx: Any, permission: str | None = permission) -> bool:
                return await self.auth_service.is_external_socket_active(ctx, permission)

            checks.append(sockets.close_ended_sessions(check))
        if checks:
            await asyncio.gather(*checks)

    async def _external_socket_loop(self) -> None:
        # One check at a time: the next sleep starts after the previous check
        # finished, so a slow adapter never stacks checks.
        while True:
            await asyncio.sleep(self.external_socket_check_interval)
            try:
                await self.recheck_external_sockets()
            except Exception:
                logger.exception("External-auth socket check error")

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
        self._api_token_store: IApiTokenStore | None = None
        self._allow_non_expiring_tokens = False
        self._read_only_users = False
        self._read_only_groups = False
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
        """Keep the audit log in ``store``: the same as setting the store of
        the audit plugin (``CliAuditBuilder().set_store(store)``)."""
        self._audit_store = store
        return self

    def set_group_store(self, store: IGroupStore) -> CliUsersBuilder:
        self._group_store = store
        return self

    def set_api_token_store(self, store: IApiTokenStore) -> CliUsersBuilder:
        """Replace the API token store (default in-memory)."""
        self._api_token_store = store
        return self

    def set_allow_non_expiring_tokens(self, allow: bool = True) -> CliUsersBuilder:
        """Allow API tokens that never expire (``expiresInDays: null``;
        default ``False``)."""
        self._allow_non_expiring_tokens = allow
        return self

    def set_read_only_users(self, read_only: bool = True) -> CliUsersBuilder:
        """The host application owns the users (e.g. a read-only
        ``IUserStore`` over its own accounts): creating, updating and deleting
        users, password changes and group membership changes answer ``501``
        without calling the stores, no ``root`` user is seeded, and the
        current session reports ``usersReadOnly: true``."""
        self._read_only_users = read_only
        return self

    def set_read_only_groups(self, read_only: bool = True) -> CliUsersBuilder:
        """The host application owns the groups: creating, updating and
        deleting groups answer ``501`` without calling the store, the built-in
        groups are not seeded, and the current session reports
        ``groupsReadOnly: true``."""
        self._read_only_groups = read_only
        return self

    def set_auth_adapter(self, adapter: ExternalAuthAdapter | None) -> CliUsersBuilder:
        """Delegate authentication to the host app (see README).

        ``adapter(request)`` returns an :class:`ExternalAuthResult` or
        ``None``, either directly or as an awaitable: plain functions,
        ``async def`` functions and objects with a sync or async ``__call__``
        are all accepted.  ``None`` turns external auth off again."""
        if adapter is not None and not callable(adapter):
            raise TypeError("auth adapter must be callable (sync or async)")
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

    def set_trust_proxy(self, trust: bool | int = True) -> CliUsersBuilder:
        """Take the client IP from ``X-Forwarded-For`` behind trusted proxies.

        ``False``/``0`` uses the socket peer (header ignored), ``True`` = 1
        trusted hop, an int ``n`` = ``n`` hops: the client is the entry at
        ``len - n`` (the rightmost for one proxy), the leftmost if there are
        fewer than ``n`` entries.  Only enable behind a reverse proxy.
        """
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

    def build(
        self,
        broadcast_fn: BroadcastFn | None = None,
        event_bus: ICliEventBus | None = None,
    ) -> CliUsersPlugin:
        """Build the plugin.  ``user:*``, ``group:*``, ``token:*`` and
        ``session:*`` events go to ``event_bus`` (default: the server's, set
        when mounted).  ``broadcast_fn`` receives JSON-encoded events (e.g.
        ``session-kicked``, and the others without a bus); it may be sync or
        async."""
        audit_builder = CliAuditBuilder()
        if self._audit_store is not None:
            audit_builder.set_store(self._audit_store)
        audit_plugin = audit_builder.build()
        auth_service = AuthService(
            self._user_store or InMemoryUserStore(),
            self._session_store or InMemorySessionStore(),
            self._credential_store or InMemoryCredentialStore(),
            audit_plugin.store,
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
                allow_non_expiring_tokens=self._allow_non_expiring_tokens,
                read_only_users=self._read_only_users,
                read_only_groups=self._read_only_groups,
            ),
            broadcast_fn=broadcast_fn,
            auth_adapter=self._auth_adapter,
            api_token_store=self._api_token_store or InMemoryApiTokenStore(),
        )
        auth_service.event_bus = event_bus
        auth_service.audit_recorded = audit_plugin.publish_recorded
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
            api_token_store=auth_service.api_token_store,
            default_admin_password=self._default_admin_password,
            cleanup_interval_ms=self._cleanup_interval_ms,
            protect_server=self._protect_server,
            audit_plugin=audit_plugin,
        )

        # The users plugin records its events in the audit plugin's store,
        # also after it joined the host's audit plugin (another store).
        def follow_audit_store(store: IAuditStore) -> None:
            auth_service.audit_store = store
            plugin.audit_store = store

        audit_plugin.on_store_changed(follow_audit_store)

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
        router.include_router(create_tokens_router(auth_service, get_auth))
        return plugin
