"""Qodalis CLI users plugin — user management, authentication, and sessions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import APIRouter

from .controllers.audit_controller import create_audit_router
from .controllers.auth_controller import create_auth_router
from .controllers.groups_controller import create_groups_router
from .controllers.sessions_controller import create_sessions_router
from .controllers.users_controller import create_users_router
from .middleware.auth_middleware import create_auth_dependencies
from .models import ExternalAuthAdapter, ExternalAuthResult, User
from .services.auth_service import AuthService
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

__all__ = [
    "AuthService",
    "ExternalAuthAdapter",
    "ExternalAuthResult",
    "IAuditStore",
    "ICredentialStore",
    "IGroupStore",
    "ISessionStore",
    "IUserStore",
    "InMemoryAuditStore",
    "InMemoryCredentialStore",
    "InMemoryGroupStore",
    "InMemorySessionStore",
    "InMemoryUserStore",
    "UsersPluginOptions",
    "UsersPluginResult",
    "build_users_plugin",
]

DEFAULT_SESSION_TIMEOUT_MS = 30 * 60 * 1000  # 30 minutes
CLEANUP_INTERVAL_S = 5 * 60  # 5 minutes


@dataclass
class UsersPluginOptions:
    """Configuration for the users plugin."""

    user_store: IUserStore | None = None
    session_store: ISessionStore | None = None
    credential_store: ICredentialStore | None = None
    audit_store: IAuditStore | None = None
    group_store: IGroupStore | None = None
    session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS
    default_admin_password: str = "root"
    auth_adapter: Optional[ExternalAuthAdapter] = None


@dataclass
class UsersPluginResult:
    """Return value from :func:`build_users_plugin`.

    Exposes ``prefix`` and ``router`` attributes so that
    ``CliServerResult.mount_plugin(result)`` works out of the box.
    """

    prefix: str = "/api/v1/qcli"
    router: APIRouter = field(default_factory=APIRouter)
    auth_service: AuthService | None = None
    user_store: IUserStore | None = None
    session_store: ISessionStore | None = None
    group_store: IGroupStore | None = None
    audit_store: IAuditStore | None = None
    _cleanup_task: asyncio.Task | None = field(default=None, repr=False)

    def dispose(self) -> None:
        if self._cleanup_task:
            try:
                self._cleanup_task.cancel()
            except RuntimeError:
                pass  # Event loop may already be closed
            self._cleanup_task = None


async def build_users_plugin(
    options: UsersPluginOptions | None = None,
    broadcast_event: Callable[[dict[str, Any]], None] | None = None,
) -> UsersPluginResult:
    """Build and return a fully wired users plugin."""

    opts = options or UsersPluginOptions()

    auth_adapter = opts.auth_adapter
    use_external_auth = auth_adapter is not None

    # 1. Create store instances (in-memory defaults, overridable)
    user_store = opts.user_store or InMemoryUserStore()
    session_store = opts.session_store or InMemorySessionStore()
    credential_store = opts.credential_store or InMemoryCredentialStore() if not use_external_auth else (opts.credential_store or InMemoryCredentialStore())
    audit_store = opts.audit_store or InMemoryAuditStore()
    group_store = opts.group_store or InMemoryGroupStore()

    # 2. Create AuthService
    auth_service = AuthService(user_store, session_store, credential_store, audit_store)

    # 3. Seed default admin user if no users exist (skip when using external auth)
    if not use_external_auth:
        existing_users = await user_store.get_users()
        if len(existing_users) == 0:
            admin_user = await user_store.create_user(
                name="root",
                email="root@localhost",
                groups=["admin"],
            )
            await auth_service.set_password(admin_user.id, opts.default_admin_password)

    # 4. Seed 'admin' group if not already present
    existing_admin_group = await group_store.get_group("admin")
    if not existing_admin_group:
        await group_store.create_group("admin", "Administrator group")

    # 5. Create auth dependencies
    get_current_session, require_admin = create_auth_dependencies(
        session_store, auth_adapter=auth_adapter,
    )

    # 6. Build the router
    router = APIRouter()
    router.include_router(create_auth_router(
        auth_service, get_current_session, external_auth=use_external_auth,
    ))
    router.include_router(create_users_router(
        user_store, auth_service, credential_store, session_store,
        get_current_session, require_admin,
    ))
    router.include_router(create_sessions_router(
        session_store, get_current_session, require_admin, broadcast_event,
    ))
    router.include_router(create_groups_router(
        group_store, user_store, get_current_session, require_admin,
    ))
    router.include_router(create_audit_router(audit_store, require_admin))

    # 7. Start cleanup task
    session_timeout_ms = opts.session_timeout_ms

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_S)
            try:
                await session_store.cleanup(session_timeout_ms)
            except Exception:
                logger.exception("Session cleanup error")

    cleanup_task: asyncio.Task | None = None
    try:
        loop = asyncio.get_running_loop()
        cleanup_task = loop.create_task(_cleanup_loop())
    except RuntimeError:
        # No running loop yet — caller should start cleanup themselves
        pass

    # 8. Return plugin result
    return UsersPluginResult(
        prefix="/api/v1/qcli",
        router=router,
        auth_service=auth_service,
        user_store=user_store,
        session_store=session_store,
        group_store=group_store,
        audit_store=audit_store,
        _cleanup_task=cleanup_task,
    )
