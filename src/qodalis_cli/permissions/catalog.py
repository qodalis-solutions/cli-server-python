"""The permission catalog: the permissions a token can be given, with a
description and a group, declared by the core, its plugins and the host.

The catalog is for choosing (the dashboard's and console's pickers, host
tokens), not for validation: ``cli`` tokens keep accepting any permission.
Entries are registered at startup and read-only afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, get_args

CliPermissionSurface = Literal["cli", "host"]

_SURFACES: tuple[str, ...] = get_args(CliPermissionSurface)
_NAME = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_.-]+$")
_MAX_DESCRIPTION = 200
_MAX_GROUP = 64


@dataclass(frozen=True)
class CliPermission:
    """A concrete permission a token can be given."""

    #: ``area:action``, lower case, e.g. ``files:read``.
    name: str
    #: One line, 1-200 characters.
    description: str
    #: The picker's heading, 1-64 characters, e.g. ``Files``.
    group: str
    #: ``cli`` for the CLI routes' permissions, ``host`` for the host
    #: application's own routes (the only ones a ``host`` token may hold).
    surface: CliPermissionSurface = "host"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "surface": self.surface,
        }


def _invalid(name: Any) -> ValueError:
    return ValueError(f"invalid permission entry: {name}")


def _name_of(entry: Any) -> Any:
    return entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", entry)


def _validated(entry: CliPermission | Mapping[str, Any], surface: str) -> CliPermission:
    if isinstance(entry, CliPermission):
        name, description, group = entry.name, entry.description, entry.group
    elif isinstance(entry, Mapping):
        name, description, group = entry.get("name"), entry.get("description"), entry.get("group")
    else:
        raise _invalid(getattr(entry, "name", entry))
    if (
        surface not in _SURFACES
        or not isinstance(name, str)
        or not _NAME.match(name)
        or not isinstance(description, str)
        or not 1 <= len(description) <= _MAX_DESCRIPTION
        or "\n" in description
        or "\r" in description
        or not isinstance(group, str)
        or not 1 <= len(group) <= _MAX_GROUP
    ):
        raise _invalid(name)
    return CliPermission(name=name, description=description, group=group, surface=surface)  # type: ignore[arg-type]


class CliPermissionCatalog:
    """The registered permissions, unique by name (case-insensitively)."""

    def __init__(self) -> None:
        self._entries: list[CliPermission] = []
        self._by_name: dict[str, CliPermission] = {}

    def add(
        self,
        entries: Iterable[CliPermission | Mapping[str, Any]],
        surface: CliPermissionSurface,
    ) -> None:
        """Register entries with the given surface (it overrides the entries'
        own).  Raises ``ValueError`` for an invalid entry or a name already
        registered (case-insensitively, checked first: ``FILES:READ`` is a
        duplicate of ``files:read``); nothing of the call is registered then."""
        added: list[CliPermission] = []
        seen: set[str] = set()
        for entry in entries:
            name = _name_of(entry)
            if isinstance(name, str) and (name.lower() in self._by_name or name.lower() in seen):
                raise ValueError(f"permission already registered: {name}")
            permission = _validated(entry, surface)
            seen.add(permission.name.lower())
            added.append(permission)
        for permission in added:
            self._entries.append(permission)
            self._by_name[permission.name.lower()] = permission

    def all(self) -> list[CliPermission]:
        """Every entry: core, then plugins, then the host.  A Python host
        registers in ``configure`` before plugins are mounted, so ``cli``
        entries are listed before ``host`` entries, each in registration
        order."""
        return [p for p in self._entries if p.surface == "cli"] + [
            p for p in self._entries if p.surface != "cli"
        ]

    def find(self, name: str) -> CliPermission | None:
        """The entry named ``name`` (case-insensitive; wildcards never match)."""
        if not isinstance(name, str):
            return None
        return self._by_name.get(name.lower())


def _core(name: str, group: str, description: str) -> CliPermission:
    return CliPermission(name=name, description=description, group=group, surface="cli")


#: The permissions core's route policy checks, registered on every server
#: (also without the filesystem, data explorer or jobs configured).
CORE_PERMISSIONS: tuple[CliPermission, ...] = (
    _core("commands:execute", "Commands", "Run commands on the server"),
    _core("shell:execute", "Commands", "Open an interactive shell on the server"),
    _core("files:read", "Files", "List, read and download files"),
    _core("files:write", "Files", "Upload, create and delete files"),
    _core("data:query", "Data", "Query the data explorer's sources"),
    _core("jobs:read", "Jobs", "See jobs and their runs"),
    _core("jobs:manage", "Jobs", "Start, stop, pause and change jobs"),
    _core("logs:read", "Server", "Read the server's logs"),
    _core("admin:access", "Server", "Open the admin dashboard and its server pages"),
    _core("config:read", "Config", "See the server's configuration (secrets stay hidden)"),
    _core("config:write", "Config", "Change the server's configuration"),
    _core("audit:read", "Audit", "Read the audit log"),
    _core("audit:clear", "Audit", "Clear the audit log"),
)
