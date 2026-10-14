"""Qodalis CLI users plugin — user management, authentication, and sessions.

Re-exports from ``qodalis_cli_users`` for backward compatibility
(``from plugins.users import build_users_plugin`` keeps working in-tree).
"""

from .qodalis_cli_users import (
    AuthService,
    ExternalAuthAdapter,
    ExternalAuthResult,
    IAuditStore,
    ICredentialStore,
    IGroupStore,
    ISessionStore,
    IUserStore,
    InMemoryAuditStore,
    InMemoryCredentialStore,
    InMemoryGroupStore,
    InMemorySessionStore,
    InMemoryUserStore,
    UsersPluginOptions,
    UsersPluginResult,
    build_users_plugin,
)

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
