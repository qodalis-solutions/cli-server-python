"""Qodalis CLI users plugin — user management, authentication, permissions,
sessions, groups and audit."""

from __future__ import annotations

from .cli_users_builder import CliUsersBuilder, CliUsersPlugin
from .errors import UsersApiError
from .models import (
    ApiToken,
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
from .server_protection import ServerProtectionMiddleware
from .shell_access import UsersShellAccess
from .stores.in_memory_api_token_store import InMemoryApiTokenStore
from .stores.in_memory_audit_store import InMemoryAuditStore
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

__all__ = [
    "ApiToken",
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
    "IApiTokenStore",
    "IAuditStore",
    "ICredentialStore",
    "IGroupStore",
    "ISessionStore",
    "IUserStore",
    "InMemoryApiTokenStore",
    "InMemoryAuditStore",
    "InMemoryCredentialStore",
    "InMemoryGroupStore",
    "InMemorySessionStore",
    "InMemoryUserStore",
    "Session",
    "ServerProtectionMiddleware",
    "SessionMetadata",
    "User",
    "UsersApiError",
    "UsersService",
    "UsersShellAccess",
    "has_permission",
    "merge_permissions",
    "permission_matches",
]
