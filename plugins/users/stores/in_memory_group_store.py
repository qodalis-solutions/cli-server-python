"""In-memory implementation of IGroupStore."""

from __future__ import annotations

import time
import uuid

from ..models import Group
from .interfaces import IGroupStore


class InMemoryGroupStore(IGroupStore):
    def __init__(self) -> None:
        self._groups: dict[str, Group] = {}  # keyed by name

    async def get_groups(self) -> list[Group]:
        return list(self._groups.values())

    async def get_group(self, name: str) -> Group | None:
        return self._groups.get(name)

    async def create_group(self, name: str, description: str | None = None) -> Group:
        if name in self._groups:
            raise ValueError(f"Group '{name}' already exists")

        group = Group(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            created_at=int(time.time() * 1000),
        )
        self._groups[name] = group
        return group

    async def delete_group(self, name: str) -> None:
        self._groups.pop(name, None)
