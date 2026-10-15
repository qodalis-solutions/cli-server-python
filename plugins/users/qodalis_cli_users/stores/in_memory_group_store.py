"""In-memory implementation of IGroupStore."""

from __future__ import annotations

import copy

from ..models import Group, now_ms
from .interfaces import IGroupStore


class InMemoryGroupStore(IGroupStore):
    """Groups keyed by lower-cased name; the name is stored as given."""

    def __init__(self) -> None:
        self._groups: dict[str, Group] = {}

    async def get_groups(self) -> list[Group]:
        return [copy.deepcopy(g) for g in self._groups.values()]

    async def get_group(self, name: str) -> Group | None:
        group = self._groups.get(name.lower())
        return copy.deepcopy(group) if group else None

    async def create_group(
        self,
        name: str,
        description: str | None = None,
        permissions: list[str] | None = None,
        *,
        built_in: bool = False,
    ) -> Group:
        key = name.lower()
        if key in self._groups:
            raise ValueError(f"group already exists: {name}")
        now = now_ms()
        group = Group(
            name=name,
            description=description,
            permissions=list(permissions or []),
            built_in=built_in,
            created_at=now,
            updated_at=now,
        )
        self._groups[key] = group
        return copy.deepcopy(group)

    async def update_group(
        self,
        name: str,
        *,
        description: str | None = None,
        permissions: list[str] | None = None,
    ) -> Group:
        group = self._groups.get(name.lower())
        if not group:
            raise KeyError(f"group not found: {name}")
        if description is not None:
            group.description = description
        if permissions is not None:
            group.permissions = list(permissions)
        group.updated_at = now_ms()
        return copy.deepcopy(group)

    async def delete_group(self, name: str) -> None:
        self._groups.pop(name.lower(), None)
