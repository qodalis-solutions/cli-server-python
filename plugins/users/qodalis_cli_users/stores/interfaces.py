"""Abstract store interfaces for the users plugin.

Implement these to persist users, sessions, credentials, groups and the audit
log in your own storage.  All business rules (permissions, cascades,
protections) live in the plugin; stores are plain persistence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from ..models import (
    AuditEntry,
    AuditQueryOptions,
    Credentials,
    Group,
    Session,
    SessionMetadata,
    User,
)


class IUserStore(ABC):
    """Persistence interface for user records."""

    @abstractmethod
    async def get_users(self) -> list[User]:
        ...

    @abstractmethod
    async def get_user(self, id_or_name: str) -> User | None:
        """Look a user up by id, or by name (case-insensitive)."""

    async def get_user_by_email(self, email: str) -> User | None:
        """Look a user up by email (case-insensitive).

        Default implementation scans :meth:`get_users`; override for
        efficiency.
        """
        lower = email.strip().lower()
        for user in await self.get_users():
            if user.email.lower() == lower:
                return user
        return None

    @abstractmethod
    async def create_user(
        self,
        name: str,
        email: str,
        groups: list[str] | None = None,
        data: dict | None = None,
        *,
        disabled: bool = False,
        home_dir: str | None = None,
        must_change_password: bool = False,
    ) -> User:
        ...

    @abstractmethod
    async def update_user(
        self,
        user_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        groups: list[str] | None = None,
        disabled: bool | None = None,
        data: dict | None = None,
        home_dir: str | None = None,
        last_login_at: int | None = None,
        must_change_password: bool | None = None,
    ) -> User:
        """Update the given fields (``None`` = unchanged).  Raises
        ``KeyError`` when the user does not exist."""

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        ...


class ISessionStore(ABC):
    """Persistence interface for sessions.

    Sessions are keyed by ``token_hash`` — the SHA-256 (hex) of the bearer
    token; raw tokens never reach the store.  Also addressable by id.
    """

    @abstractmethod
    async def create_session(
        self,
        user_id: str,
        token_hash: str,
        metadata: SessionMetadata,
        *,
        user_name: str = "",
    ) -> Session:
        ...

    @abstractmethod
    async def get_session(self, token_hash: str) -> Session | None:
        ...

    @abstractmethod
    async def get_session_by_id(self, session_id: str) -> Session | None:
        ...

    @abstractmethod
    async def get_active_sessions(self) -> list[Session]:
        ...

    @abstractmethod
    async def delete_session(self, token_hash: str) -> None:
        ...

    @abstractmethod
    async def delete_session_by_id(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def delete_user_sessions(self, user_id: str) -> None:
        ...

    @abstractmethod
    async def update_last_activity(self, token_hash: str) -> None:
        ...

    @abstractmethod
    async def cleanup(self, max_age_ms: int) -> None:
        """Delete sessions idle for longer than ``max_age_ms``."""


class ICredentialStore(ABC):
    """Persistence interface for user credentials."""

    @abstractmethod
    async def set_password(self, user_id: str, hash_: str, salt: str) -> None:
        ...

    @abstractmethod
    async def get_credentials(self, user_id: str) -> Credentials | None:
        ...

    @abstractmethod
    async def delete_credentials(self, user_id: str) -> None:
        ...


class IAuditStore(ABC):
    """Persistence interface for audit log entries."""

    @abstractmethod
    async def log(self, entry: AuditEntry) -> None:
        ...

    @abstractmethod
    async def get_entries(self, options: AuditQueryOptions | None = None) -> list[AuditEntry]:
        """Entries newest-first, filtered by ``options``."""

    @abstractmethod
    async def clear(self) -> None:
        ...


class IGroupStore(ABC):
    """Persistence interface for user groups.  Lookups are case-insensitive."""

    @abstractmethod
    async def get_groups(self) -> list[Group]:
        ...

    @abstractmethod
    async def get_group(self, name: str) -> Group | None:
        ...

    @abstractmethod
    async def create_group(
        self,
        name: str,
        description: str | None = None,
        permissions: list[str] | None = None,
        *,
        built_in: bool = False,
    ) -> Group:
        """Raises ``ValueError`` when a group with that name exists."""

    @abstractmethod
    async def update_group(
        self,
        name: str,
        *,
        description: str | None = None,
        permissions: list[str] | None = None,
    ) -> Group:
        """Update the given fields (``None`` = unchanged).  Raises
        ``KeyError`` when the group does not exist."""

    @abstractmethod
    async def delete_group(self, name: str) -> None:
        ...


__all__ = [
    "IAuditStore",
    "ICredentialStore",
    "IGroupStore",
    "ISessionStore",
    "IUserStore",
]
