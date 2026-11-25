"""``admin``: server administration from the terminal (status, command modules, WebSocket clients, logs)."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
    ICliCommandProcessor,
    get_positionals,
)

from ..controllers.status_controller import DEFAULT_HISTORY_MINUTES, MAX_HISTORY_MINUTES
from ..services.log_ring_buffer import LOG_LEVELS, LogRingBuffer
from ..services.module_registry import ModuleRegistry
from ..services.status_history import StatusHistory, summarize_status_history
from ..services.time_range import RangeError, parse_since

#: The permission the ``admin`` command needs (the dashboard's).
ADMIN_ACCESS_PERMISSION = "admin:access"
#: The permission ``admin logs`` needs besides ``admin:access`` (as the log socket and event audience do).
LOGS_READ_PERMISSION = "logs:read"
#: The module of the ``admin`` command (``admin plugins disable`` refuses it: that would disable this command).
ADMIN_MODULE_NAME = "admin"

_JSON = CliCommandParameterDescriptor("json", "Print the result as JSON", False, "boolean")
MAX_LOG_LINES = 1000


@dataclass
class AdminCommandDeps:
    """What the ``admin`` command reads: the same services the admin routes use."""

    status: Callable[[], dict[str, Any]]
    status_history: StatusHistory
    module_registry: ModuleRegistry
    log_buffer: LogRingBuffer
    event_socket_manager: Any
    installed_plugins: Callable[[], list[Any]]


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or (isinstance(value, str) and value.lower() in ("true", "1", ""))


def _num(n: Any) -> str:
    """A number as JavaScript prints it (``110``, ``3.5``)."""
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def short_time(iso: str) -> str:
    """``2026-11-25T10:04:05.123Z`` -> ``2026-11-25 10:04:05``."""
    return re.sub(r"(\.\d+)?(Z|[+-]00:00)$", "", iso.replace("T", " ", 1))


def format_duration(seconds: float) -> str:
    """7384 -> ``2 h 3 min``; under a minute ``42 s``; from a day ``3 d 4 h``."""
    s = max(0, int(math.floor(seconds)))
    if s < 60:
        return f"{s} s"
    d, h, m = s // 86_400, (s % 86_400) // 3600, (s % 3600) // 60
    if d > 0:
        return f"{d} d {h} h"
    return f"{h} h {m} min" if h > 0 else f"{m} min"


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Aligned columns: every column but the last padded to its widest cell."""
    widths = [max(len(h), *(len(r[i]) if i < len(r) else 0 for r in rows)) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  ".join(c if i == len(cells) - 1 else c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    return "\n".join([line(headers), *(line(r) for r in rows)])


def _int_arg(command: CliProcessCommand, names: list[str], fallback: int, maximum: int, label: str) -> int | str:
    """A whole number flag from 1 to ``maximum``: ``fallback`` when absent, an error text when invalid."""
    args = command.args or {}
    raw = next((args[n] for n in names if args.get(n) is not None), None)
    if raw is None:
        return fallback
    try:
        n = float(raw) if raw is not True else math.nan
    except (TypeError, ValueError):
        n = math.nan
    if not n.is_integer() or n < 1 or n > maximum:
        return f"Error: {label} must be a whole number from 1 to {maximum}"
    return int(n)


def _pct(n: Any) -> str:
    return f"{_num(n)}%"


def _mb(n: Any) -> str:
    return f"{_num(n)} MB"


class AdminStatusProcessor(CliCommandProcessor):
    command = "status"
    description = (
        "Shows the server status: version, uptime, platform, memory, CPU, connections; "
        "--history summarizes the recent samples"
    )
    parameters = [
        CliCommandParameterDescriptor(
            "history", "Summarize the samples of the last minutes instead (count, min/avg/max memory and CPU)",
            False, "boolean",
        ),
        CliCommandParameterDescriptor(
            "minutes",
            f"With --history: how many minutes back (1-{MAX_HISTORY_MINUTES}, default {DEFAULT_HISTORY_MINUTES})",
            False, "number",
        ),
        _JSON,
    ]
    examples = [
        CliCommandExample("admin status", "The status now"),
        CliCommandExample("admin status --history --minutes 15", "Memory and CPU over the last 15 minutes"),
    ]

    def __init__(self, deps: AdminCommandDeps) -> None:
        self._deps = deps

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        if _flag(command, "history"):
            return self._history(command)
        if (command.args or {}).get("minutes") is not None:
            return "Error: --minutes goes with --history"
        status = self._deps.status()
        plugins = self._deps.installed_plugins()
        server = plugins[0] if plugins else None
        if _flag(command, "json"):
            info = {"id": server.id, "version": server.version or None} if server is not None else None
            return _json({"server": info, **status})
        cpu = status.get("cpuUsagePercent")
        features = status.get("enabledFeatures") or []
        rows = [
            ("Server", f"{server.id}{' ' + server.version if server.version else ''}" if server is not None else "unknown"),
            ("Platform", f"{status['platform']} {status['platformVersion']} ({status['os']})"),
            ("Uptime", f"{format_duration(status['uptimeSeconds'])} (since {short_time(status['startedAt'])})"),
            ("Memory", _mb(status["memoryUsageMb"])),
            ("CPU", "n/a" if cpu is None else _pct(cpu)),
            ("Connections", f"{status['activeWsConnections']} WebSocket, {status['activeShellSessions']} shell"),
            ("Commands", str(status["registeredCommands"])),
            ("Jobs", str(status["registeredJobs"])),
            ("Features", ", ".join(features) if features else "none"),
        ]
        return "\n".join(f"{k.ljust(12)}{v}" for k, v in rows)

    def _history(self, command: CliProcessCommand) -> str:
        minutes = _int_arg(command, ["minutes"], DEFAULT_HISTORY_MINUTES, MAX_HISTORY_MINUTES, "--minutes")
        if isinstance(minutes, str):
            return minutes
        history = self._deps.status_history
        summary = summarize_status_history(history.history(minutes))
        interval = _num(history.interval)
        if _flag(command, "json"):
            return _json({"minutes": minutes, "intervalSeconds": history.interval, **summary})
        if summary["count"] == 0:
            return f"No status samples in the last {minutes} min."

        def r(x: dict[str, Any], unit: Callable[[Any], str]) -> str:
            return f"min {unit(x['min'])}  avg {unit(x['avg'])}  max {unit(x['max'])}"

        cpu = summary.get("cpuPercent")
        return "\n".join([
            f"Last {minutes} min: {summary['count']} sample(s) every {interval} s, "
            f"{short_time(summary['from'])} to {short_time(summary['to'])}",
            f"{'Memory'.ljust(12)}{r(summary['memoryMb'], _mb)}",
            f"{'CPU'.ljust(12)}{r(cpu, _pct) if cpu else 'n/a'}",
            f"{'Peak'.ljust(12)}{summary['maxWsConnections']} WebSocket, {summary['maxShellSessions']} shell",
        ])


def _plugin_rows(modules: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [m["id"], m["name"], m.get("version") or "-", "enabled" if m["enabled"] else "disabled", str(m["processorCount"])]
        for m in modules
    ]


class AdminPluginsListProcessor(CliCommandProcessor):
    command = "list"
    aliases = ["ls"]
    description = "Lists the command modules: id, name, version, enabled or disabled, commands"
    parameters = [_JSON]

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        modules = self._registry.list()
        if _flag(command, "json"):
            return _json(modules)
        if not modules:
            return "No command modules registered."
        return format_table(["ID", "Name", "Version", "Status", "Commands"], _plugin_rows(modules))


class AdminPluginsSwitchProcessor(CliCommandProcessor):
    command = ""
    description = ""
    examples: list[CliCommandExample] = []
    value_required = True

    def __init__(self, registry: ModuleRegistry, enable: bool) -> None:
        self._registry = registry
        self._enable = enable
        self.command = "enable" if enable else "disable"
        self.description = (
            "Enables a command module (its commands run again)" if enable
            else "Disables a command module: its commands are refused until it is enabled"
        )
        self.examples = [CliCommandExample(
            f"admin plugins {self.command} weather", f"{'Enable' if enable else 'Disable'} the weather module",
        )]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        words = get_positionals(command)
        if len(words) != 1:
            return f"Error: name one module: admin plugins {self.command} <id> (admin plugins lists them)"
        module_id = words[0]
        try:
            target = self._registry.get(module_id)
        except KeyError:
            return f"Error: unknown module: {module_id} (admin plugins lists them)"
        if not self._enable and target["id"] == ADMIN_MODULE_NAME:
            return "Error: the admin module cannot be disabled from the terminal: it would disable this command"
        result = self._registry.set_enabled(module_id, self._enable)
        assert result is not None
        module, changed = result
        state = "enabled" if self._enable else "disabled"
        if not changed:
            return f"{module['id']} is already {state}."
        n = module["processorCount"]
        return f"{'Enabled' if self._enable else 'Disabled'} {module['id']} ({n} command{'' if n == 1 else 's'})."


class AdminPluginsProcessor(CliCommandProcessor):
    command = "plugins"
    aliases = ["modules"]
    description = "Command modules: list them, enable or disable one"
    parameters = [_JSON]

    def __init__(self, registry: ModuleRegistry) -> None:
        self._list = AdminPluginsListProcessor(registry)
        self._processors: list[ICliCommandProcessor] = [
            self._list, AdminPluginsSwitchProcessor(registry, True), AdminPluginsSwitchProcessor(registry, False),
        ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return await self._list.handle_async(command, cancellation_event)


class AdminClientsProcessor(CliCommandProcessor):
    command = "clients"
    description = "Lists the connected WebSocket clients: id, type, connected since, address"
    parameters = [_JSON]

    def __init__(self, event_socket_manager: Any) -> None:
        self._manager = event_socket_manager

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        clients = list(self._manager.get_clients())
        if _flag(command, "json"):
            return _json(clients)
        if not clients:
            return "No WebSocket clients connected."
        rows = [
            [str(c.get("id", "")), str(c.get("type", "")), short_time(str(c.get("connectedAt", ""))), c.get("remoteAddress") or "-"]
            for c in clients
        ]
        return "\n".join([f"{len(clients)} client(s)", format_table(["ID", "Type", "Connected", "Address"], rows)])


def format_log_line(entry: dict[str, Any]) -> str:
    """One entry as a line of ``admin logs``: time, level, [source], message."""
    source = entry.get("source") or ""
    return f"{short_time(entry['timestamp'])} {entry['level'].ljust(5)} {f'[{source}] ' if source else ''}{entry['message']}"


class AdminLogsProcessor(CliCommandProcessor):
    command = "logs"
    description = "Shows the latest server log entries (oldest first), filtered by level, text, source and time"
    required_permissions = [LOGS_READ_PERMISSION]
    parameters = [
        CliCommandParameterDescriptor("level", "Only these levels, comma-separated: debug, info, warn, error", False, "string"),
        CliCommandParameterDescriptor(
            "search", "Only entries with this text in the message or the source (case-insensitive)", False, "string",
        ),
        CliCommandParameterDescriptor("service", "Only entries of this source", False, "string"),
        CliCommandParameterDescriptor(
            "since", "Only entries since a duration ago (30s, 5m, 1h, 7d) or an ISO 8601 date", False, "string",
        ),
        CliCommandParameterDescriptor(
            "limit", f"How many entries (1-{MAX_LOG_LINES}, default 50)", False, "number", aliases=["n"], default_value=50,
        ),
        _JSON,
    ]
    examples = [
        CliCommandExample("admin logs", "The last 50 entries"),
        CliCommandExample("admin logs --level warn,error --since 1h", "Warnings and errors of the last hour"),
        CliCommandExample("admin logs --search timeout -n 200", "The last 200 entries mentioning timeout"),
    ]

    def __init__(self, log_buffer: LogRingBuffer) -> None:
        self._buffer = log_buffer

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        limit = _int_arg(command, ["limit", "n"], 50, MAX_LOG_LINES, "--limit")
        if isinstance(limit, str):
            return limit
        args = command.args or {}
        values: dict[str, Optional[str]] = {}
        for name in ("level", "search", "service", "since"):
            v = args.get(name)
            if v is None:
                values[name] = None
                continue
            if v is True or str(v).strip() == "":
                return f"Error: --{name} needs a value"
            values[name] = str(v).strip()
        level = values["level"]
        if level is not None:
            bad = [
                part for part in (p.strip().upper() for p in level.split(","))
                if part and ("WARN" if part == "WARNING" else part) not in LOG_LEVELS
            ]
            if bad:
                return f"Error: unknown level {', '.join(b.lower() for b in bad)} (debug, info, warn, error)"
            level = re.sub(r"\bwarning\b", "warn", level, flags=re.IGNORECASE)
        since: Optional[datetime] = None
        if values["since"] is not None:
            try:
                since = parse_since(values["since"], "--since")
            except RangeError as exc:
                return f"Error: {exc}"
        entries, _ = self._buffer.query(level=level, search=values["search"], since=since, limit=10**9)
        service = values["service"]
        if service is not None:
            # The entries of this source (the logger name)
            entries = [e for e in entries if (e.get("source") or "").lower() == service.lower()]
        total = len(entries)
        # The newest ``limit`` entries, oldest first (like tail)
        page = entries[max(0, total - limit):]
        if _flag(command, "json"):
            return _json({"entries": page, "total": total})
        if not page:
            return "No log entries match."
        header = [f"Last {len(page)} of {total} entries"] if total > len(page) else []
        return "\n".join([*header, *(format_log_line(e) for e in page)])


class CliAdminCommandProcessor(CliCommandProcessor):
    """``admin``: ``status [--history]``, ``plugins [list|enable|disable <id>]``, ``clients``, ``logs [...]``.
    Needs ``admin:access`` on a protected server; ``logs`` also ``logs:read``.  Server settings are the core
    ``config`` command."""

    command = "admin"
    description = "Server administration: status, command modules, WebSocket clients, logs"
    module = ADMIN_MODULE_NAME
    icon = "🛠️"
    required_permissions = [ADMIN_ACCESS_PERMISSION]

    def __init__(self, deps: AdminCommandDeps) -> None:
        self._status = AdminStatusProcessor(deps)
        self._processors: list[ICliCommandProcessor] = [
            self._status,
            AdminPluginsProcessor(deps.module_registry),
            AdminClientsProcessor(deps.event_socket_manager),
            AdminLogsProcessor(deps.log_buffer),
        ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        """``admin`` alone: the status."""
        return await self._status.handle_async(command, cancellation_event)


class AdminModule(CliModule):
    """The ``admin`` terminal command, registered by ``mount_plugin``."""

    name = ADMIN_MODULE_NAME
    description = "Server administration from the terminal: status, command modules, WebSocket clients, logs"
    icon = "🛠️"

    def __init__(self, processor: CliAdminCommandProcessor, version: str) -> None:
        self._processor = processor
        self._version = version

    @property
    def version(self) -> str:  # type: ignore[override]
        return self._version

    @property
    def processors(self) -> list[ICliCommandProcessor]:  # type: ignore[override]
        return [self._processor]
