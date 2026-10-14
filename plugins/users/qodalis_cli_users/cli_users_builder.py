"""Fluent builder for configuring and creating the users plugin.

Usage::

    users_plugin = (
        CliUsersBuilder()
        .set_default_admin_password("admin")
        .set_session_timeout(30 * 60 * 1000)
        .build(broadcast_fn=result.event_socket_manager.broadcast_message)
    )
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
from typing import Optional

from fastapi import APIRouter

from .controllers.audit_controller import create_audit_router
from .controllers.groups_controller import create_groups_router
from .controllers.sessions_controller import create_sessions_router
from .controllers.users_controller import create_users_router
from .middleware.auth_middleware import create_auth_dependencies
from .models import ExternalAuthAdapter
from .services.auth_service import (
    DEFAULT_LOCKOUT_MS,
    DEFAULT_MAX_LOGIN_ATTEMPTS,
    DEFAULT_MIN_PASSWORD_LENGTH,
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
    default_admin_password: str = "root"
    cleanup_interval_ms: int = DEFAULT_CLEANUP_INTERVAL_MS
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
        self._default_admin_password = "root"
        self._session_timeout_ms = DEFAULT_SESSION_TIMEOUT_MS
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

    def set_default_admin_password(self, password: str) -> CliUsersBuilder:
        self._default_admin_password = password
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
                min_password_length=self._min_password_length,
                max_login_attempts=self._max_login_attempts,
                lockout_ms=self._lockout_ms,
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
        )

        # Requests made before startup() (e.g. apps without a lifespan) still
        # see the seeded built-ins.
        get_auth, require_permission = create_auth_dependencies(
            auth_service, ensure_ready=plugin.ensure_seeded,
        )
        router.include_router(create_sessions_router(
            auth_service, get_auth, require_permission, ensure_ready=plugin.ensure_seeded,
        ))
        router.include_router(create_users_router(
            auth_service, users_service, get_auth, require_permission,
        ))
        router.include_router(create_groups_router(
            auth_service, users_service, get_auth, require_permission,
        ))
        router.include_router(create_audit_router(auth_service, require_permission))
        return plugin
