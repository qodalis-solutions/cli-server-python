"""Data models for the users plugin."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------

@dataclass
class User:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    disabled: bool = False
    data: dict[str, Any] | None = None
    last_login_at: int | None = None
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "groups": self.groups,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.disabled:
            result["disabled"] = True
        if self.data is not None:
            result["data"] = self.data
        if self.last_login_at is not None:
            result["lastLoginAt"] = self.last_login_at
        return result


@dataclass
class Credentials:
    user_id: str = ""
    password_hash: str = ""
    salt: str = ""
    last_changed: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token: str = ""
    user_id: str = ""
    user: User = field(default_factory=User)
    login_time: int = field(default_factory=lambda: int(time.time() * 1000))
    last_activity: int = field(default_factory=lambda: int(time.time() * 1000))
    metadata: SessionMetadata | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "token": self.token,
            "userId": self.user_id,
            "user": self.user.to_dict(),
            "loginTime": self.login_time,
            "lastActivity": self.last_activity,
        }
        if self.metadata:
            result["metadata"] = self.metadata.to_dict()
        return result


@dataclass
class SessionMetadata:
    remote_address: str | None = None
    user_agent: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.remote_address is not None:
            result["remoteAddress"] = self.remote_address
        if self.user_agent is not None:
            result["userAgent"] = self.user_agent
        return result


@dataclass
class AuditEntry:
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    event: str = ""
    username: str = ""
    details: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "event": self.event,
            "username": self.username,
        }
        if self.details is not None:
            result["details"] = self.details
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
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "createdAt": self.created_at,
        }
        if self.description is not None:
            result["description"] = self.description
        return result


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    name: str
    email: str
    password: str | None = None
    groups: list[str] | None = None
    data: dict[str, Any] | None = None


class UpdateUserRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    groups: list[str] | None = None
    disabled: bool | None = None
    data: dict[str, Any] | None = None


class PasswordChangeRequest(BaseModel):
    currentPassword: str | None = None  # noqa: N815 — camelCase for API compat
    newPassword: str  # noqa: N815


class VerifyRequest(BaseModel):
    userId: str  # noqa: N815
    password: str


class CreateGroupRequest(BaseModel):
    name: str
    description: str | None = None
