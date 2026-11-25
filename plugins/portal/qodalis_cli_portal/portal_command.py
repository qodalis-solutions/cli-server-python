"""``portal``: the documentation portal in the terminal."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
    ICliCommandProcessor,
)
from qodalis_cli_server_abstractions.config import CliConfigPermissions

from .site_resolver import PORTAL_SITE_SOURCES

#: What ``portal --json`` prints: ``portal.json`` (docs with its page count) and where the site is
#: (``site``: ``path``, ``found``, ``source``, ``dir``).
PortalStatus = dict[str, Any]

PORTAL_BRANDING_HINT = (
    "Change the branding with config set portal.branding.<key> <value> (name, logoUrl, themeMode, footerLinks)."
)

_JSON = CliCommandParameterDescriptor("json", "Print the status as JSON (portal.json and the site)", False, "boolean")


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or (isinstance(value, str) and value.lower() in ("true", "1", ""))


def format_portal_status(status: PortalStatus) -> str:
    """The status as text: the mount, the site, the sections and the branding."""
    site = status["site"]
    lines = [f"Portal: {site['path']}"]
    if site["found"] and site["source"]:
        lines.append(f"Site: found, {PORTAL_SITE_SOURCES[site['source']]} ({site['dir']})")
    else:
        lines.append(
            "Site: not found, the portal is not served (install @qodalis/cli-server-portal or call set_site_dir)"
        )
    docs, api = status.get("docs"), status.get("api")
    if not docs and not api:
        lines.append('Sections: none (the site shows "Nothing published yet")')
    else:
        lines.append("Sections:")
        if docs:
            count = docs.get("pageCount")
            pages = "" if count is None else f", {count} {'page' if count == 1 else 'pages'}"
            lines.append(f"  docs  {docs['title']}{pages} ({docs['pages']})")
        if api:
            lines.append(f"  api   {api['title']} ({api['openapi']})")
    links = ", ".join(f"{l['label']} ({l['url']})" for l in status["footerLinks"]) or "(none)"
    logo = status["logo"]
    if logo is None:
        logo_text = "(none)"
    elif logo.startswith("data:"):
        logo_text = f"{logo[: logo.find(',') + 1]}… ({len(logo)} characters)"
    else:
        logo_text = logo
    lines += [
        "Branding:",
        f"  name         {status['name']}",
        f"  logoUrl      {logo_text}",
        f"  themeMode    {status['theme']['mode']}",
        f"  footerLinks  {links}",
        "",
        PORTAL_BRANDING_HINT,
    ]
    return "\n".join(lines)


class PortalStatusProcessor(CliCommandProcessor):
    command = "status"
    description = "The portal: its path, the site, the sections and the branding"
    parameters = [_JSON]
    examples = [
        CliCommandExample("portal status", "The portal at a glance"),
        CliCommandExample("portal status --json", "portal.json and the site as JSON"),
    ]

    def __init__(self, status: Callable[[], PortalStatus]) -> None:
        self._status = status

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        status = self._status()
        return json.dumps(status, indent=2, ensure_ascii=False) if _flag(command, "json") else format_portal_status(status)


class CliPortalCommandProcessor(CliCommandProcessor):
    """``portal`` (= ``portal status``) ``[--json]``: needs ``config:read`` (it shows the portal's
    settings, as ``config get portal`` does); the portal itself stays public.  Branding is changed
    with ``config set``."""

    command = "portal"
    description = f"Documentation portal: path, site, sections and branding. {PORTAL_BRANDING_HINT}"
    module = "portal"
    icon = "🌐"
    required_permissions = [CliConfigPermissions.READ]
    parameters = [_JSON]
    examples = [
        CliCommandExample("portal", "The portal at a glance"),
        CliCommandExample("portal --json", "portal.json and the site as JSON"),
        CliCommandExample('config set portal.branding.name "RateBell API"', "Rename the site (no restart)"),
        CliCommandExample("config set portal.branding.themeMode dark", "Open the site in the dark theme"),
    ]

    def __init__(self, status: Callable[[], PortalStatus]) -> None:
        self._status_processor = PortalStatusProcessor(status)
        self._processors: list[ICliCommandProcessor] = [self._status_processor]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return await self._status_processor.handle_async(command, cancellation_event)


class PortalModule(CliModule):
    """The ``portal`` terminal command, registered by ``mount_plugin``."""

    name = "portal"
    description = "The documentation portal in the terminal: path, site, sections and branding"
    icon = "🌐"

    def __init__(self, processor: CliPortalCommandProcessor, version: str) -> None:
        self._processor = processor
        self._version = version

    @property
    def version(self) -> str:  # type: ignore[override]
        return self._version

    @property
    def processors(self) -> list[ICliCommandProcessor]:  # type: ignore[override]
        return [self._processor]
