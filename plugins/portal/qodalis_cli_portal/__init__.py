"""Qodalis CLI portal plugin — the prebuilt portal site
(``@qodalis/cli-server-portal``) at one mount, with ``portal.json``: the
guides of the docs plugin and the API reference of the public-api plugin."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🧭"

from .cli_portal_builder import DEFAULT_PATH, CliPortalBuilder, CliPortalPlugin
from .site_resolver import resolve_site_dir

__all__ = [
    "CliPortalBuilder",
    "CliPortalPlugin",
    "DEFAULT_PATH",
    "resolve_site_dir",
]
