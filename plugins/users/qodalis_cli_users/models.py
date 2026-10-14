"""Data models for the users plugin.

Internal records are dataclasses; their ``to_dict`` methods produce the
camelCase JSON shapes defined by the server contract shared with the node and
.NET servers.  Request bodies are pydantic models.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from starlette.requests import Request

from pydantic import BaseModel, ConfigDict


def now_ms() -> int:
    """Current time in epoch milliseconds."""
    return int(time.time() * 1000)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Internal records
# ---------------------------------------------------------------------------

@dataclass
class User:
    id: str = field(default_factory=_new_id)
    name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    disabled: bool = False
    home_dir: str | None = None
    data: dict[str, Any] | None = None
    last_login_at: int | None = None
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "groups": list(self.groups),
            "disabled": bool(self.disabled),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.home_dir is not None:
            result["homeDir"] = self.home_dir
        if self.last_login_at is not None:
            result["lastLoginAt"] = self.last_login_at
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class Credentials:
    user_id: str = ""
    #: Encoded hash, ``pbkdf2$<iterations>$<saltB64>$<hashB64>``.
    password_hash: str = ""
    #: Salt (base64).  Also embedded in ``password_hash``; kept for store
    #: implementations that persist it separately.
    salt: str = ""
    last_changed: int = field(default_factory=now_ms)


@dataclass
class SessionMetadata:
    remote_address: str | None = None
    user_agent: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.user_agent is not None:
            result["userAgent"] = self.user_agent
        if self.remote_address is not None:
            result["remoteAddress"] = self.remote_address
        return result


@dataclass
class Session:
    """A stored login session.

    References the user by id only; the live user record is resolved from the
    user store on every request.  ``user_name`` is a snapshot taken at login,
    used only as a fallback when the user can no longer be resolved.
    """

    id: str = field(default_factory=_new_id)
    token: str = ""
    user_id: str = ""
    user_name: str = ""
    created_at: int = field(default_factory=now_ms)
    last_activity: int = field(default_factory=now_ms)
    metadata: SessionMetadata = field(default_factory=SessionMetadata)

    @property
    def login_time(self) -> int:
        return self.created_at

    def expires_at(self, timeout_ms: int) -> int:
        return self.last_activity + timeout_ms

    def to_listing_dict(
        self,
        timeout_ms: int,
        *,
        user_name: str | None = None,
        current: bool | None = None,
    ) -> dict[str, Any]:
        """Listing shape — never includes the token."""
        result: dict[str, Any] = {
            "id": self.id,
            "userId": self.user_id,
            "userName": user_name if user_name is not None else self.user_name,
            "createdAt": self.created_at,
            "lastActivity": self.last_activity,
            "expiresAt": self.expires_at(timeout_ms),
            "metadata": (self.metadata or SessionMetadata()).to_dict(),
        }
        if current is not None:
            result["current"] = current
        return result


@dataclass
class AuditEntry:
    id: str = field(default_factory=_new_id)
    timestamp: int = field(default_factory=now_ms)
    event: str = ""
    username: str = ""
    details: str | None = None
    actor: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "timestamp": self.timestamp,
            "event": self.event,
            "username": self.username,
        }
        if self.details is not None:
            result["details"] = self.details
        if self.actor is not None:
            result["actor"] = self.actor
        if self.metadata is not None:
            result["metadata"] = self.metadata
        return result


@dataclass
class AuditQueryOptions:
    limit: int | None = None
    event: str | None = None
    username: str | None = None


@dataclass
class Group:
    id: str = field(default_factory=_new_id)
    name: str = ""
    description: str | None = None
    permissions: list[str] = field(default_factory=list)
    built_in: bool = False
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "permissions": list(self.permissions),
            "builtIn": bool(self.built_in),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.description is not None:
            result["description"] = self.description
        return result


# ---------------------------------------------------------------------------
# Request bodies (camelCase field names, as sent by the client)
# ---------------------------------------------------------------------------

class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LoginRequest(_Body):
    username: str
    password: str


class CreateUserRequest(_Body):
    name: str
    email: str
    groups: list[str] | None = None
    password: str | None = None
    disabled: bool | None = None
    homeDir: str | None = None  # noqa: N815
    data: dict[str, Any] | None = None


class UpdateUserRequest(_Body):
    name: str | None = None
    email: str | None = None
    groups: list[str] | None = None
    disabled: bool | None = None
    homeDir: str | None = None  # noqa: N815
    data: dict[str, Any] | None = None


class PasswordChangeRequest(_Body):
    password: str | None = None
    currentPassword: str | None = None  # noqa: N815
    #: Deprecated alias of ``password`` (pre-contract clients).
    newPassword: str | None = None  # noqa: N815


class VerifyPasswordRequest(_Body):
    password: str


class CreateGroupRequest(_Body):
    name: str
    description: str | None = None
    permissions: list[str] | None = None


class UpdateGroupRequest(_Body):
    description: str | None = None
    permissions: list[str] | None = None


# ---------------------------------------------------------------------------
# External auth adapter
# ---------------------------------------------------------------------------

@dataclass
class ExternalAuthResult:
    """Result returned by an external auth adapter.

    ``is_admin`` adds the built-in ``admin`` group to the user's effective
    groups (the adapter's ``user`` object is never mutated).
    """

    user: User
    is_admin: bool = False


# (Request) -> ExternalAuthResult | None  (may also be async)
ExternalAuthAdapter = Callable[["Request"], Optional[ExternalAuthResult]]
