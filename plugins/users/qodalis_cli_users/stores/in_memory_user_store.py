"""In-memory implementation of IUserStore."""

from __future__ import annotations

import copy

from ..models import User, now_ms
from .interfaces import IUserStore


class InMemoryUserStore(IUserStore):
    """Keeps users in a dict.  Returns copies so callers never alias the
    stored records (changes must go through :meth:`update_user`)."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    async def get_users(self) -> list[User]:
        return [copy.deepcopy(u) for u in self._users.values()]

    def _find(self, id_or_name: str) -> User | None:
        if id_or_name in self._users:
            return self._users[id_or_name]
        lower = id_or_name.lower()
        for user in self._users.values():
            if user.name.lower() == lower:
                return user
        return None

    async def get_user(self, id_or_name: str) -> User | None:
        user = self._find(id_or_name)
        return copy.deepcopy(user) if user else None

    async def create_user(
        self,
        name: str,
        email: str,
        groups: list[str] | None = None,
        data: dict | None = None,
        *,
        disabled: bool = False,
        home_dir: str | None = None,
    ) -> User:
        lower = name.lower()
        for user in self._users.values():
            if user.name.lower() == lower:
                raise ValueError(f"user already exists: {name}")

        now = now_ms()
        user = User(
            name=name,
            email=email,
            groups=list(groups or []),
            disabled=disabled,
            home_dir=home_dir,
            data=copy.deepcopy(data),
            created_at=now,
            updated_at=now,
        )
        self._users[user.id] = user
        return copy.deepcopy(user)

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
    ) -> User:
        user = self._users.get(user_id)
        if not user:
            raise KeyError(f"user not found: {user_id}")

        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        if groups is not None:
            user.groups = list(groups)
        if disabled is not None:
            user.disabled = disabled
        if data is not None:
            user.data = copy.deepcopy(data)
        if home_dir is not None:
            user.home_dir = home_dir
        if last_login_at is not None:
            user.last_login_at = last_login_at
        user.updated_at = now_ms()
        return copy.deepcopy(user)

    async def delete_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)
