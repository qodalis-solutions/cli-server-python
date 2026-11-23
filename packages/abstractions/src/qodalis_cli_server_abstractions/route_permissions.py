"""Permissions a plugin declares for its routes (``route_permissions`` on a mounted plugin)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CliRoutePermission:
    """On a server protected by the users plugin, a request under ``path`` needs ``read`` (GET and HEAD) or
    ``write`` (any other method; ``read`` when unset).  The longest declared path wins, so a plugin can narrow
    a route below another's.  ``path`` must be under the plugin's prefix."""

    path: str
    read: str
    write: str | None = None
