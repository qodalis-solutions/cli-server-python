"""Qodalis CLI portal plugin — the prebuilt portal site
(``@qodalis/cli-server-portal``) at one mount, with ``portal.json``: the
guides of the docs plugin and the API reference of the public-api plugin."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🧭"

from .cli_portal_builder import DEFAULT_PATH, SITE_NOT_FOUND, CliPortalBuilder, CliPortalPlugin
from .portal_command import (
    PORTAL_BRANDING_HINT,
    CliPortalCommandProcessor,
    PortalModule,
    PortalStatus,
    format_portal_status,
)
from .portal_config_provider import (
    PortalBranding,
    PortalSiteFacts,
    create_portal_config_provider,
    footer_links_error,
)
from .site_resolver import PORTAL_SITE_SOURCES, PortalSiteSource, resolve_site, resolve_site_dir

__all__ = [
    "CliPortalBuilder",
    "CliPortalPlugin",
    "DEFAULT_PATH",
    "SITE_NOT_FOUND",
    "resolve_site_dir",
    "resolve_site",
    "PortalSiteSource",
    "PORTAL_SITE_SOURCES",
    "PortalBranding",
    "PortalSiteFacts",
    "create_portal_config_provider",
    "footer_links_error",
    "CliPortalCommandProcessor",
    "PortalModule",
    "PortalStatus",
    "format_portal_status",
    "PORTAL_BRANDING_HINT",
]
