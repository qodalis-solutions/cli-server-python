"""Permission matching — same semantics as ``cliPermissionMatches`` in
``@qodalis/cli-core``.

Permission strings are ``resource:action``.  A trailing ``*`` segment matches
anything at that level and below (``*``, ``users:*``); a ``*`` elsewhere
matches exactly one segment (``*:read`` matches ``users:read`` only).
Matching is case-insensitive.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

USERS_READ = "users:read"
USERS_CREATE = "users:create"
USERS_UPDATE = "users:update"
USERS_DELETE = "users:delete"
GROUPS_READ = "groups:read"
GROUPS_CREATE = "groups:create"
GROUPS_UPDATE = "groups:update"
GROUPS_DELETE = "groups:delete"
SESSIONS_READ = "sessions:read"
SESSIONS_REVOKE = "sessions:revoke"
AUDIT_READ = "audit:read"
AUDIT_CLEAR = "audit:clear"
TOKENS_READ = "tokens:read"
TOKENS_REVOKE = "tokens:revoke"

ADMIN_GROUP = "admin"
USERS_GROUP = "users"

#: The users plugin's entries of the permission catalog (surface ``cli``),
#: added to the server's catalog when the plugin is mounted.
USERS_PERMISSION_ENTRIES: tuple[dict[str, str], ...] = (
    {"name": USERS_READ, "description": "See users and their permissions", "group": "Users"},
    {"name": USERS_CREATE, "description": "Create users", "group": "Users"},
    {"name": USERS_UPDATE, "description": "Change users, their groups and passwords", "group": "Users"},
    {"name": USERS_DELETE, "description": "Delete users", "group": "Users"},
    {"name": GROUPS_READ, "description": "See groups", "group": "Groups"},
    {"name": GROUPS_CREATE, "description": "Create groups", "group": "Groups"},
    {"name": GROUPS_UPDATE, "description": "Change groups and their permissions", "group": "Groups"},
    {"name": GROUPS_DELETE, "description": "Delete groups", "group": "Groups"},
    {"name": SESSIONS_READ, "description": "See every user's sessions", "group": "Sessions"},
    {"name": SESSIONS_REVOKE, "description": "End other users' sessions", "group": "Sessions"},
    {"name": TOKENS_READ, "description": "See every user's API tokens", "group": "Tokens"},
    {"name": TOKENS_REVOKE, "description": "Revoke other users' API tokens", "group": "Tokens"},
)


@runtime_checkable
class PermissionCatalog(Protocol):
    """The server's permission catalog (``qodalis_cli.CliPermissionCatalog``,
    ``server.builder.permissions``), seen structurally: the plugin does not
    import core code newer than the core versions it supports.  Entries
    expose ``name``, ``description``, ``group``, ``surface`` and
    ``to_dict()``."""

    def add(self, entries: Iterable[Any], surface: str) -> None: ...

    def all(self) -> list[Any]: ...

    def find(self, name: str) -> Any | None: ...


@dataclass(frozen=True)
class _CatalogEntry:
    name: str
    description: str
    group: str
    surface: str = "host"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "surface": self.surface,
        }


class LocalPermissionCatalog:
    """The plugin's own catalog, used until it is mounted on a server with a
    catalog (an older core, or the plugin's router included on its own).

    Not a general-purpose catalog: it holds the plugin's fixed entries only,
    with no validation and no all-or-nothing ``add``."""

    def __init__(self) -> None:
        self._entries: list[_CatalogEntry] = []

    def add(self, entries: Iterable[Any], surface: str) -> None:
        for entry in entries:
            get = entry.get if isinstance(entry, Mapping) else (lambda k, e=entry: getattr(e, k, None))
            name = get("name")
            if self.find(name) is not None:
                raise ValueError(f"permission already registered: {name}")
            self._entries.append(_CatalogEntry(name, get("description"), get("group"), surface))

    def all(self) -> list[Any]:
        return list(self._entries)

    def find(self, name: str) -> Any | None:
        if not isinstance(name, str):
            return None
        key = name.lower()
        return next((e for e in self._entries if e.name.lower() == key), None)


def users_permission_catalog() -> LocalPermissionCatalog:
    """A local catalog holding the users plugin's entries."""
    catalog = LocalPermissionCatalog()
    catalog.add(USERS_PERMISSION_ENTRIES, "cli")
    return catalog


def permission_matches(granted: str, required: str) -> bool:
    g = granted.strip().lower().split(":")
    r = required.strip().lower().split(":")
    for i, seg in enumerate(g):
        # A trailing '*' grants everything below; elsewhere it matches
        # exactly one segment.
        if seg == "*" and i == len(g) - 1:
            return True
        if i >= len(r) or (seg != "*" and seg != r[i]):
            return False
    return len(g) == len(r)


def has_permission(granted: Iterable[str] | None, required: str) -> bool:
    return any(permission_matches(p, required) for p in (granted or []))


def merge_permissions(*lists: Iterable[str] | None) -> list[str]:
    """Union of permission lists, de-duplicated case-insensitively."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for p in lst or []:
            key = p.strip()
            if key and key.lower() not in seen:
                seen.add(key.lower())
                out.append(key)
    return out
