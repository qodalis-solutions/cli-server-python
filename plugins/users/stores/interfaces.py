"""Abstract store interfaces for the users plugin."""

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
        ...

    @abstractmethod
    async def create_user(
        self,
        name: str,
        email: str,
        groups: list[str] | None = None,
        data: dict | None = None,
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
    ) -> User:
        ...

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        ...


class ISessionStore(ABC):
    """Persistence interface for sessions."""

    @abstractmethod
    async def create_session(
        self,
        user_id: str,
        token: str,
        user: User,
        metadata: SessionMetadata,
    ) -> Session:
        ...

    @abstractmethod
    async def get_session(self, token: str) -> Session | None:
        ...

    @abstractmethod
    async def get_session_by_id(self, session_id: str) -> Session | None:
        ...

    @abstractmethod
    async def get_active_sessions(self) -> list[Session]:
        ...

    @abstractmethod
    async def delete_session(self, token: str) -> None:
        ...

    @abstractmethod
    async def delete_session_by_id(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def delete_user_sessions(self, user_id: str) -> None:
        ...

    @abstractmethod
    async def update_last_activity(self, token: str) -> None:
        ...

    @abstractmethod
    async def cleanup(self, max_age_ms: int) -> None:
        ...


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
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...


class IGroupStore(ABC):
    """Persistence interface for user groups."""

    @abstractmethod
    async def get_groups(self) -> list[Group]:
        ...

    @abstractmethod
    async def get_group(self, name: str) -> Group | None:
        ...

    @abstractmethod
    async def create_group(self, name: str, description: str | None = None) -> Group:
        ...

    @abstractmethod
    async def delete_group(self, name: str) -> None:
        ...
