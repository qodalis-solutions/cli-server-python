"""Permission matching — same semantics as ``cliPermissionMatches`` in
``@qodalis/cli-core``.

Permission strings are ``resource:action``.  A trailing ``*`` segment matches
anything at that level and below (``*``, ``users:*``); a ``*`` elsewhere
matches exactly one segment (``*:read`` matches ``users:read`` only).
Matching is case-insensitive.
"""

from __future__ import annotations

from collections.abc import Iterable

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

ADMIN_GROUP = "admin"
USERS_GROUP = "users"


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
