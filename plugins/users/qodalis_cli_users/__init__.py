"""Qodalis CLI users plugin — user management, authentication, permissions,
sessions, groups and audit."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .cli_users_builder import CliUsersBuilder, CliUsersPlugin
from .errors import UsersApiError
from .models import (
    AuditEntry,
    AuditQueryOptions,
    Credentials,
    ExternalAuthAdapter,
    ExternalAuthResult,
    Group,
    Session,
    SessionMetadata,
    User,
)
from .permissions import has_permission, merge_permissions, permission_matches
from .services.auth_service import (
    DEFAULT_SESSION_TIMEOUT_MS,
    AuthContext,
    AuthService,
    AuthServiceOptions,
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

__all__ = [
    "AuditEntry",
    "AuditQueryOptions",
    "AuthContext",
    "AuthService",
    "AuthServiceOptions",
    "CliUsersBuilder",
    "CliUsersPlugin",
    "Credentials",
    "ExternalAuthAdapter",
    "ExternalAuthResult",
    "Group",
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
    "Session",
    "SessionMetadata",
    "User",
    "UsersApiError",
    "UsersPluginOptions",
    "UsersPluginResult",
    "UsersService",
    "build_users_plugin",
    "has_permission",
    "merge_permissions",
    "permission_matches",
]

#: Deprecated alias of :class:`CliUsersPlugin`.
UsersPluginResult = CliUsersPlugin


@dataclass
class UsersPluginOptions:
    """Deprecated options for :func:`build_users_plugin`; use
    :class:`CliUsersBuilder`."""

    user_store: IUserStore | None = None
    session_store: ISessionStore | None = None
    credential_store: ICredentialStore | None = None
    audit_store: IAuditStore | None = None
    group_store: IGroupStore | None = None
    session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS
    default_admin_password: str = "root"
    auth_adapter: Optional[ExternalAuthAdapter] = None


async def build_users_plugin(
    options: UsersPluginOptions | None = None,
    broadcast_event: Callable[[str], Any] | None = None,
) -> CliUsersPlugin:
    """Deprecated: use :class:`CliUsersBuilder` and call ``startup()`` from
    the app lifespan.  Builds the plugin and runs ``startup()`` on the
    current loop."""
    warnings.warn(
        "build_users_plugin() is deprecated; use CliUsersBuilder().build() and "
        "await plugin.startup() in the app lifespan",
        DeprecationWarning,
        stacklevel=2,
    )
    opts = options or UsersPluginOptions()
    builder = (
        CliUsersBuilder()
        .set_default_admin_password(opts.default_admin_password)
        .set_session_timeout(opts.session_timeout_ms)
        .set_auth_adapter(opts.auth_adapter)
    )
    if opts.user_store:
        builder.set_user_store(opts.user_store)
    if opts.session_store:
        builder.set_session_store(opts.session_store)
    if opts.credential_store:
        builder.set_credential_store(opts.credential_store)
    if opts.audit_store:
        builder.set_audit_store(opts.audit_store)
    if opts.group_store:
        builder.set_group_store(opts.group_store)
    plugin = builder.build(broadcast_event)
    await plugin.startup()
    return plugin
