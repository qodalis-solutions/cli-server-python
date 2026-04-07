"""In-memory implementation of IUserStore."""

from __future__ import annotations

import time
import uuid

from ..models import User
from .interfaces import IUserStore


class InMemoryUserStore(IUserStore):
    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    async def get_users(self) -> list[User]:
        return list(self._users.values())

    async def get_user(self, id_or_name: str) -> User | None:
        # Try by id first
        if id_or_name in self._users:
            return self._users[id_or_name]
        # Fall back to case-insensitive name lookup
        lower = id_or_name.lower()
        for user in self._users.values():
            if user.name.lower() == lower:
                return user
        return None

    async def create_user(
        self,
        name: str,
        email: str,
        groups: list[str] | None = None,
        data: dict | None = None,
    ) -> User:
        # Check for duplicate name (case-insensitive)
        lower = name.lower()
        for user in self._users.values():
            if user.name.lower() == lower:
                raise ValueError(f"User with name '{name}' already exists")

        now = int(time.time() * 1000)
        user = User(
            id=str(uuid.uuid4()),
            name=name,
            email=email,
            groups=groups or [],
            data=data,
            created_at=now,
            updated_at=now,
        )
        self._users[user.id] = user
        return user

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
        user = self._users.get(user_id)
        if not user:
            raise ValueError(f"User with id '{user_id}' not found")

        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        if groups is not None:
            user.groups = groups
        if disabled is not None:
            user.disabled = disabled
        if data is not None:
            user.data = data
        user.updated_at = int(time.time() * 1000)
        return user

    async def delete_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)
