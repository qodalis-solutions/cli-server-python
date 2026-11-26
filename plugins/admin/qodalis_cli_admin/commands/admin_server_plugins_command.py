"""``admin plugins search|installed|install|update|remove``: server plugins from the marketplace (Python packages
installed at runtime, running after a restart)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliCommandRefusal,
    CliProcessCommand,
    ICliCommandProcessor,
    get_positionals,
)

from ..services.dashboard_marketplace import DashboardMarketplaceActor, DashboardMarketplaceError
from ..services.server_plugin_marketplace import (
    PLUGINS_INSTALL_PERMISSION,
    SERVER_PLUGIN_PLATFORM,
    SERVER_PLUGIN_PLATFORM_FILTERS,
    ServerPluginMarketplace,
)

_JSON = CliCommandParameterDescriptor("json", "Print the result as JSON", False, "boolean")


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or (isinstance(value, str) and value.lower() in ("true", "1", ""))


def _short_time(iso: str) -> str:
    return re.sub(r"(\.\d+)?(Z|[+-]00:00)$", "", iso.replace("T", " ", 1)) if iso else "-"


def _actor() -> DashboardMarketplaceActor:
    try:
        from qodalis_cli import get_cli_caller  # the server package, optional here
    except ImportError:  # pragma: no cover
        return DashboardMarketplaceActor(user_name="anonymous", anonymous=True)
    caller = get_cli_caller()
    if caller is None:
        return DashboardMarketplaceActor(user_name="anonymous", anonymous=True)
    return DashboardMarketplaceActor(user_name=caller.user_name, user_id=caller.user_id)


async def _call(fn: Callable[[], Awaitable[str]]) -> str:
    try:
        return await fn()
    except DashboardMarketplaceError as exc:
        text = f"Error: {exc.message}"
        # Sign-in required / missing permission: exit code 1, as on Node
        return CliCommandRefusal(text) if exc.status == 403 else text


def _spec(text: str) -> tuple[str, Any]:
    """``jobs@1.0.0b31`` (or ``jobs==1.0.0b31``) -> name and version."""
    if "==" in text:
        name, _, version = text.partition("==")
        return name, version or None
    at = text.rfind("@")
    return (text[:at], text[at + 1:]) if at > 0 else (text, None)


def _restart_line(r: dict[str, Any]) -> str:
    return (
        "Restart the server to apply it: admin restart (or the dashboard)."
        if r.get("restartRequired") else "No restart needed: the server already runs this."
    )


def _needs_line(r: dict[str, Any]) -> list[str]:
    needs = r.get("needsConfiguration") or []
    return [f"Settings to fill in after the restart: {', '.join(needs)} (config, or the dashboard)."] if needs else []


def _table(headers: list[str], rows: list[list[str]]) -> str:
    from .admin_command import format_table

    return format_table(headers, rows)


class AdminServerPluginsSearchProcessor(CliCommandProcessor):
    command = "search"
    description = "Searches the marketplace's server plugins: this server's package, version, platforms, whether it can be installed"
    accepts_value = True
    parameters = [
        CliCommandParameterDescriptor(
            "platform", f"Whose plugins: {', '.join(SERVER_PLUGIN_PLATFORM_FILTERS)} (default this: the ones for this server)", False, "string",
        ),
        _JSON,
    ]
    examples = [
        CliCommandExample("admin plugins search jobs", "Server plugins about jobs"),
        CliCommandExample("admin plugins search --platform=all", "Every server plugin, also the ones for other platforms"),
    ]

    def __init__(self, marketplace: ServerPluginMarketplace) -> None:
        self._marketplace = marketplace

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        async def run() -> str:
            query = " ".join(get_positionals(command))
            platform = (command.args or {}).get("platform")
            result = await self._marketplace.search(query, page_size=50, platform=None if platform is None else str(platform))
            if _flag(command, "json"):
                return _json(result)
            items = result["items"]
            if not items:
                return f'No server plugins match "{query}".' if query else "The marketplace lists no server plugins."
            rows = []
            for p in items:
                platform = p.get("platform") or None
                code = p.get("installed") if isinstance(p.get("installed"), dict) and p["installed"].get("source") == "code" else None
                if code:
                    status = "added in code" + (f" {code['version']}" if code.get("version") else "")
                    if p.get("updateAvailable"):
                        status += ", newer version in the marketplace"
                elif platform and platform.get("installed"):
                    v = platform["installed"]
                    status = f"installed {v}, update available" if p.get("updateAvailable") else f"installed {v}"
                elif p.get("allowed"):
                    needs = p.get("needsConfiguration") or []
                    status = f"installable (then set {', '.join(needs)})" if needs else "installable"
                else:
                    reason = f" ({p['autoInstallReason']})" if p.get("autoInstallReason") else ""
                    status = f"blocked: {p.get('blockedReason')}{reason}"
                publisher = p.get("publisher") if isinstance(p.get("publisher"), dict) else {}
                platforms = [x for x in [SERVER_PLUGIN_PLATFORM, *p.get("otherPlatforms", [])] if x != SERVER_PLUGIN_PLATFORM or platform]
                rows.append([
                    p["name"],
                    platform["package"] if platform else f"(not for {SERVER_PLUGIN_PLATFORM})",
                    str((platform or {}).get("latest") or "-"),
                    ", ".join(platforms),
                    f"{publisher.get('name') or '-'}{' (verified)' if publisher.get('verified') else ''}",
                    status,
                ])
            more = [f"{len(items)} of {result['total']} shown"] if result["total"] > len(items) else []
            return "\n".join([_table(["Plugin", "Package", "Version", "Platforms", "Publisher", "Status"], rows), *more])

        return await _call(run)


class AdminServerPluginsInstalledProcessor(CliCommandProcessor):
    command = "installed"
    description = "Lists the server plugins installed from the marketplace: package, version, status (loaded, pending restart, failed, needs configuration)"
    parameters = [_JSON]

    def __init__(self, marketplace: ServerPluginMarketplace) -> None:
        self._marketplace = marketplace

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        async def run() -> str:
            plugins = await self._marketplace.installed(check_updates=True)
            if _flag(command, "json"):
                return _json(plugins)
            info = self._marketplace.info()
            url, install = info["marketplaceUrl"], info["install"]
            if not plugins:
                return f"No server plugins installed from the marketplace ({url}, install: {install}; admin plugins search <query> finds some)."

            def status(p: dict[str, Any]) -> str:
                if p["status"] == "pending":
                    return "pending restart"
                if p["status"] == "removed":
                    return "removed, running until a restart"
                if p["status"] == "needs-configuration":
                    return f"needs configuration: {', '.join(p.get('missing') or [])}"
                return f"{p['status']}: {p['error']}" if p.get("error") else p["status"]

            rows = [
                [
                    p["name"], p["package"], f"{p['version']}{' (update available)' if p.get('updateAvailable') else ''}",
                    status(p), _short_time(p["installedAt"]), p["installedBy"] or "-",
                ]
                for p in plugins
            ]
            return "\n".join([
                f"{len(plugins)} server plugin(s) from {url} (install: {install})",
                _table(["Plugin", "Package", "Version", "Status", "Installed", "By"], rows),
            ])

        return await _call(run)


class AdminServerPluginsInstallProcessor(CliCommandProcessor):
    command = "install"
    description = "Installs a server plugin from the marketplace (this server's package, the newest compatible version or name@version); runs after a restart"
    accepts_value = True
    value_required = True
    required_permissions = [PLUGINS_INSTALL_PERMISSION]
    examples = [
        CliCommandExample("admin plugins install weather", "The newest compatible version"),
        CliCommandExample("admin plugins install jobs@1.0.0b31", "A given version"),
    ]

    def __init__(self, marketplace: ServerPluginMarketplace) -> None:
        self._marketplace = marketplace

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        async def run() -> str:
            words = get_positionals(command)
            if len(words) != 1:
                return "Error: name one plugin: admin plugins install <name>[@version]"
            name, version = _spec(words[0])
            r = await self._marketplace.install_plugin(name, version, _actor())
            return "\n".join([f"Installed {r['name']}: {r['package']}=={r['version']}.", _restart_line(r), *_needs_line(r)])

        return await _call(run)


class AdminServerPluginsUpdateProcessor(CliCommandProcessor):
    command = "update"
    description = "Updates an installed server plugin to the newest compatible version (or name@version); runs after a restart"
    accepts_value = True
    value_required = True
    required_permissions = [PLUGINS_INSTALL_PERMISSION]

    def __init__(self, marketplace: ServerPluginMarketplace) -> None:
        self._marketplace = marketplace

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        async def run() -> str:
            words = get_positionals(command)
            if len(words) != 1:
                return "Error: name one plugin: admin plugins update <name>[@version]"
            name, version = _spec(words[0])
            r = await self._marketplace.update_plugin(name, version, _actor())
            if r.get("changed") is False:
                return f"{r['name']} is already at {r['version']}."
            return "\n".join([f"Updated {r['name']} from {r['previousVersion']} to {r['version']}.", _restart_line(r), *_needs_line(r)])

        return await _call(run)


class AdminServerPluginsRemoveProcessor(CliCommandProcessor):
    command = "remove"
    aliases = ["uninstall"]
    description = "Removes a server plugin installed from the marketplace; it runs until a restart"
    accepts_value = True
    value_required = True
    required_permissions = [PLUGINS_INSTALL_PERMISSION]

    def __init__(self, marketplace: ServerPluginMarketplace) -> None:
        self._marketplace = marketplace

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        async def run() -> str:
            words = get_positionals(command)
            if len(words) != 1:
                return "Error: name one plugin: admin plugins remove <name>"
            r = await self._marketplace.remove_plugin(words[0], _actor())
            return "\n".join([f"Removed {r['name']} ({r['package']}=={r['version']}).", _restart_line(r)])

        return await _call(run)


def server_plugin_processors(marketplace: ServerPluginMarketplace) -> list[ICliCommandProcessor]:
    """``admin plugins search|installed|install|update|remove``."""
    return [
        AdminServerPluginsSearchProcessor(marketplace),
        AdminServerPluginsInstalledProcessor(marketplace),
        AdminServerPluginsInstallProcessor(marketplace),
        AdminServerPluginsUpdateProcessor(marketplace),
        AdminServerPluginsRemoveProcessor(marketplace),
    ]
