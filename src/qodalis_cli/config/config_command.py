"""``config``: the server's configuration from the terminal."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandProcessor,
    get_positionals,
)
from qodalis_cli_server_abstractions.config import (
    CliConfigCaller,
    CliConfigContext,
    CliConfigError,
    CliConfigPermissions,
    ICliConfigService,
    cli_config_context,
    describe_cli_config_store_line,
    parse_cli_config_input,
)

from ..services.cli_command_executor_service import command_auth

#: ``--expected-version``: the section version a change is based on.  Not
#: ``--version``: the web CLI answers that global flag itself (it prints the
#: command's version), so it would never reach the server; raw API callers
#: may still send it.
_EXPECTED_VERSION = CliCommandParameterDescriptor(
    "expected-version", "The section version the change is based on (refused when stale)", False, "string",
)


def _expected_version_of(command: CliProcessCommand) -> Optional[str]:
    args = command.args or {}
    value = args.get("expected-version", args.get("version"))
    return str(value) if value is not None and value is not True else None


def config_context_of_auth(auth: Any, via: str) -> CliConfigContext:
    """The config context of a server auth context (``None``: a server without authentication)."""
    if auth is None:
        return cli_config_context(None, via)
    user = getattr(auth, "user", None)
    caller = CliConfigCaller(
        user_id=getattr(user, "id", None) or getattr(auth, "user_id", None),
        user_name=getattr(user, "name", None) or getattr(auth, "user_name", None),
        permissions=tuple(getattr(auth, "permissions", None) or ()),
    )
    return cli_config_context(caller, via, has_permission=lambda p: bool(auth.can(p)))


def _context() -> CliConfigContext:
    """The caller of the command (protected servers), as a config context."""
    return config_context_of_auth(command_auth.get(), "terminal")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def format_config_value(entry: dict[str, Any]) -> str:
    """A value as printed: strings as they are, anything else as JSON; secrets as set / not set."""
    if entry.get("sensitive"):
        return "(set)" if entry.get("isSet") else "(not set)"
    value = entry.get("value")
    if value is None:
        return "(none)"
    return value if isinstance(value, str) else _json(value)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, CliConfigError):
        version = f" (current version {exc.version})" if exc.version is not None else ""
        lines = [f"Error: {exc.message}{version}"]
        lines.extend(f"  {key}: {message}" for key, message in (exc.errors or {}).items())
        return "\n".join(lines)
    return f"Error: {exc}"


async def _resolve_path(service: ICliConfigService, path: str) -> tuple[dict, Optional[dict], Optional[str]]:
    """Resolves ``provider[.section[.key]]``: the provider id has no dots; the
    section is the longest section id the rest starts with (ids and keys may
    contain dots)."""
    provider_id, _, rest = path.partition(".")
    provider = await service.get(provider_id, _context())
    if not rest:
        return provider, None, None
    candidates = [s for s in provider["sections"] if rest == s["id"] or rest.startswith(s["id"] + ".")]
    if not candidates:
        raise ValueError(f"unknown config section: {provider_id}.{rest}")
    section = max(candidates, key=lambda s: len(s["id"]))
    key = rest[len(section["id"]) + 1:] if len(rest) > len(section["id"]) else None
    return provider, section, key or None


def _describe_section(provider: str, section: dict) -> list[str]:
    title = f"  {section['title']}" if section["title"] != section["id"] else ""
    version = f"  (version {section['version']})" if section["version"] is not None else "  (read-only)"
    lines = [f"{provider}.{section['id']}{title}{version}"]
    width = min(32, max([0, *(len(e["key"]) for e in section["entries"])]))
    for e in section["entries"]:
        flags = [e["type"], e["source"], *([] if e["mutable"] else ["read-only"]), *(["restart"] if e["requiresRestart"] else [])]
        lines.append(f"  {e['key'].ljust(width)}  {format_config_value(e)}  [{', '.join(flags)}]")
    if not section["entries"]:
        lines.append("  (no settings)")
    return lines


def _fmt_number(n: Any) -> str:
    return str(int(n)) if isinstance(n, float) and n.is_integer() else str(n)


def _describe_entry(provider: str, section: dict, e: dict) -> str:
    lines = [f"{provider}.{section['id']}.{e['key']}", f"  Label         {e['label']}"]
    if e["description"]:
        lines.append(f"  Description   {e['description']}")
    lines.append(f"  Type          {e['type']}")
    lines.append(f"  Value         {format_config_value(e)}")
    if not e["sensitive"]:
        lines.append(f"  Default       {format_config_value({'value': e['default'], 'sensitive': False})}")
    lines.append(f"  Source        {e['source']}")
    lines.append(f"  Mutable       {'yes' if e['mutable'] else 'no'}{' (restart required)' if e['requiresRestart'] else ''}")
    v = e.get("validation")
    if v:
        rules = [
            "required" if v.get("required") else "",
            f"min {_fmt_number(v['min'])}" if v.get("min") is not None else "",
            f"max {_fmt_number(v['max'])}" if v.get("max") is not None else "",
            f"pattern {v['pattern']}" if v.get("pattern") is not None else "",
            f"one of {', '.join(v['options'])}" if v.get("options") else "",
        ]
        rules = [r for r in rules if r]
        if rules:
            lines.append(f"  Validation    {'; '.join(rules)}")
    if section["version"] is not None:
        lines.append(f"  Version       {section['version']}")
    return "\n".join(lines)


class _ConfigSubcommand(CliCommandProcessor):
    value_required: Optional[bool] = True

    def __init__(self, service: ICliConfigService) -> None:
        self.service = service


class ConfigListProcessor(_ConfigSubcommand):
    command = "list"
    description = "Lists where changes are saved, the config providers and their sections"
    value_required = None
    required_permissions = [CliConfigPermissions.READ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            providers = await self.service.list(_context())
            lines: list[str] = [f"Store: {describe_cli_config_store_line(self.service.describe_store())}"]
            if not providers:
                return "\n".join([*lines, "No config providers."])
            for p in providers:
                sources = (p.get("storage") or {}).get("sources") or []
                notes = "; ".join(n for n in ("" if p["writable"] else "read-only", f"from {', '.join(sources)}" if sources else "") if n)
                lines.append(f"{p['id']}  {p['title']}{f'  ({notes})' if notes else ''}")
                for s in p["sections"]:
                    version = f"  version {s['version']}" if s["version"] is not None else ""
                    lines.append(f"  {s['id'].ljust(16)} {str(len(s['entries'])).rjust(3)} setting(s){version}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - printed to the terminal
            return _error_text(exc)


class ConfigGetProcessor(_ConfigSubcommand):
    command = "get"
    description = "Shows a provider, a section or one setting: config get <provider>[.<section>[.<key>]]"
    required_permissions = [CliConfigPermissions.READ]
    examples = [
        CliCommandExample("config get cors", "Every section of the cors provider"),
        CliCommandExample("config get cors.cors.allowedOrigins", "One setting with its default, source and rules"),
    ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            positionals = get_positionals(command)
            if not positionals:
                return "Error: usage: config get <provider>[.<section>[.<key>]]"
            provider, section, key = await _resolve_path(self.service, positionals[0])
            if section is None:
                description = f"  {provider['description']}" if provider["description"] else ""
                lines = [f"{provider['id']}  {provider['title']}{description}"]
                for s in provider["sections"]:
                    lines.extend(_describe_section(provider["id"], s))
                return "\n".join(lines)
            if key is None:
                return "\n".join(_describe_section(provider["id"], section))
            entry = next((e for e in section["entries"] if e["key"] == key), None)
            if entry is None:
                return f"Error: unknown setting: {provider['id']}.{section['id']}.{key}"
            return _describe_entry(provider["id"], section, entry)
        except Exception as exc:  # noqa: BLE001
            return _error_text(exc)


def _changed_text(verb: str, provider: str, section: str, key: str, result: dict, shown: str) -> str:
    lines = [f"{verb} {provider}.{section}.{key}{f' = {shown}' if shown else ''} (version {result['version']})"]
    if result["requiresRestart"]:
        lines.append("Restart the server for the change to take effect.")
    return "\n".join(lines)


class ConfigSetProcessor(_ConfigSubcommand):
    command = "set"
    description = "Changes a setting: config set <provider>.<section>.<key> <value> [--expected-version v]"
    required_permissions = [CliConfigPermissions.WRITE]
    parameters = [_EXPECTED_VERSION]
    examples = [
        CliCommandExample(
            "config set cors.cors.allowedOrigins https://app.example.com,http://localhost:*",
            "A list: comma-separated or a JSON array",
        ),
        CliCommandExample(
            "config set mcp.mcp.readOnly true --expected-version 3", "Refused when the section changed since version 3",
        ),
        CliCommandExample('config set custom.custom.motd "Hello there"', "A free key of the custom provider"),
    ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            positionals = get_positionals(command)
            if len(positionals) < 2:
                return "Error: usage: config set <provider>.<section>.<key> <value> [--expected-version v]"
            provider, section, key = await _resolve_path(self.service, positionals[0])
            if section is None or key is None:
                return "Error: name a setting: <provider>.<section>.<key>"
            text = " ".join(positionals[1:])
            entry = next((e for e in section["entries"] if e["key"] == key), None)
            try:
                if entry is not None:
                    value = parse_cli_config_input(entry["type"], text)
                else:
                    # A new key of a free section: JSON when it parses (numbers, booleans, lists), else the text
                    try:
                        value = json.loads(text)
                    except ValueError:
                        value = text
            except ValueError as exc:
                return f"Error: validation failed\n  {key}: {exc}"
            request: dict[str, Any] = {"changes": {key: value}}
            version = _expected_version_of(command)
            if version:
                request["version"] = version
            result = await self.service.update(provider["id"], section["id"], request, _context())
            updated = next((e for e in result["section"]["entries"] if e["key"] == key), None)
            shown = format_config_value(updated) if updated is not None else ""
            return _changed_text("Updated", provider["id"], section["id"], key, result, shown)
        except Exception as exc:  # noqa: BLE001
            return _error_text(exc)


class ConfigResetProcessor(_ConfigSubcommand):
    command = "reset"
    description = (
        "Resets a setting to its default (clears a secret, removes a free key): config reset <provider>.<section>.<key>"
    )
    required_permissions = [CliConfigPermissions.WRITE]
    parameters = [_EXPECTED_VERSION]
    examples = [
        CliCommandExample("config reset cors.cors.allowedOrigins", "Back to the origins the server was started with"),
    ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            positionals = get_positionals(command)
            if not positionals:
                return "Error: usage: config reset <provider>.<section>.<key>"
            provider, section, key = await _resolve_path(self.service, positionals[0])
            if section is None or key is None:
                return "Error: name a setting: <provider>.<section>.<key>"
            request: dict[str, Any] = {"changes": {key: None}}
            version = _expected_version_of(command)
            if version:
                request["version"] = version
            result = await self.service.update(provider["id"], section["id"], request, _context())
            updated = next((e for e in result["section"]["entries"] if e["key"] == key), None)
            shown = format_config_value(updated) if updated is not None else ""
            return _changed_text("Reset", provider["id"], section["id"], key, result, shown)
        except Exception as exc:  # noqa: BLE001
            return _error_text(exc)


class CliConfigCommandProcessor(CliCommandProcessor):
    """``config``: the server's configuration from the terminal.  ``list``,
    ``get <provider>[.<section>[.<key>]]``, ``set <provider>.<section>.<key>
    <value> [--expected-version v]``, ``reset <provider>.<section>.<key>``.
    ``config:read`` for the command, ``config:write`` for set and reset;
    secrets are accepted, never printed."""

    command = "config"
    description = "The server's configuration: list providers, show, change and reset settings"
    module = "config"
    icon = "⚙️"
    required_permissions = [CliConfigPermissions.READ]

    def __init__(self, service: ICliConfigService) -> None:
        self.service = service
        self._list = ConfigListProcessor(service)
        self._processors: list[ICliCommandProcessor] = [
            self._list, ConfigGetProcessor(service), ConfigSetProcessor(service), ConfigResetProcessor(service),
        ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return await self._list.handle_async(command, cancellation_event)
