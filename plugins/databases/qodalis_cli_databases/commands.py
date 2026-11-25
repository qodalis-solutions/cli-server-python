"""Terminal commands: ``databases`` (alias ``db``) — ``list``, ``stats <db>``, ``migrations <db>``,
``up <db>``, ``down <db> --to <id>``, ``runs <db>``."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
    get_positional_text,
)

from .databases import DATABASES_MIGRATE, DATABASES_READ, CliDatabases
from .runner import CliMigrationError, CliMigrationRun, CliMigrationRunOptions

_TO = CliCommandParameterDescriptor(
    "to", "Migration to stop at (id or source:id; down: base for everything)", False, "string",
)
_DRY_RUN = CliCommandParameterDescriptor("dry-run", "Show the SQL instead of running it", False, "boolean")
_FORCE = CliCommandParameterDescriptor("force", "Run although migrations changed or went missing", False, "boolean")


_LIMIT = CliCommandParameterDescriptor(
    "limit", "How many runs to show (1-100, default 20)", False, "number", aliases=["n"], default_value=20,
)
_JSON = CliCommandParameterDescriptor("json", "Print the runs as JSON", False, "boolean")


def limit_of(command: CliProcessCommand, fallback: int, maximum: int) -> int | str:
    """``--limit`` / ``-n``: a whole number from 1 to ``maximum``; ``fallback`` when not given; an error text when invalid."""
    args = command.args or {}
    raw = args.get("limit", args.get("n"))
    if raw is None or raw is True:
        return fallback
    try:
        n = float(raw) if not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        n = None
    if n is None or not n.is_integer() or n < 1 or n > maximum:
        return f"Error: --limit must be a whole number from 1 to {maximum}"
    return int(n)


def _short_time(iso: str | None) -> str:
    """``2026-11-25T10:04:05.123Z`` -> ``2026-11-25 10:04:05``."""
    if not iso:
        return ""
    text = iso.replace("T", " ", 1)
    return re.sub(r"(\.\d+)?(Z|[+-]00:00)$", "", text)


def format_run_line(run: CliMigrationRun) -> str:
    """One run as a line of ``databases runs``: time, direction, status, count, trigger/actor, error."""
    who = f"{run.trigger} ({run.actor})" if run.actor else run.trigger
    columns = [_short_time(run.started_at).ljust(19), run.direction.ljust(4), run.status.ljust(9), str(len(run.applied)).rjust(5), who]
    return "  ".join(p for p in [" ".join(columns), f"to {run.target}" if run.target else "", run.error or ""] if p)


def _database_of(databases: CliDatabases, command: CliProcessCommand) -> str:
    name = get_positional_text(command) or (databases.databases[0].name if len(databases.databases) == 1 else "")
    if not name:
        raise CliMigrationError(f"name the database: {', '.join(d.name for d in databases.databases)}", 400)
    return name


def format_bytes(value: Any) -> str:
    """``1.5 MB``-style size."""
    if not isinstance(value, (int, float)):
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _describe_run(run: CliMigrationRun) -> str:
    verb = "Applied" if run.direction == "up" else "Rolled back"
    lines = [f"{verb} {len(run.applied)} migration(s) ({run.status})"]
    lines += [f"  {a['source']}:{a['id']}  {a['name']}  {a['durationMs']} ms" for a in run.applied]
    if run.error:
        lines.append(f"Failed: {run.error}")
    return "\n".join(lines)


def describe_stats(stats: dict[str, Any]) -> str:
    """The stats as text (terminal and MCP)."""
    lines = [f"{stats['name']} ({stats['dialect']})"]
    server = stats.get("server") or {}
    if server.get("version"):
        uptime = server.get("uptimeSeconds")
        lines.append(f"  Server:      {server['version']}" + (f", up {uptime // 3600}h {uptime % 3600 // 60}m" if isinstance(uptime, int) else ""))
    size = stats.get("size") or {}
    if size.get("bytes") is not None:
        parts = [f"{label} {format_bytes(size[key])}" for key, label in (("dataBytes", "data"), ("indexBytes", "indexes"), ("logBytes", "log")) if size.get(key) is not None]
        lines.append(f"  Size:        {format_bytes(size['bytes'])}" + (f" ({', '.join(parts)})" if parts else ""))
    conns = stats.get("connections") or {}
    if conns:
        lines.append("  Connections: " + ", ".join(f"{conns[k]} {k}" for k in ("active", "open", "max") if conns.get(k) is not None))
    counters = stats.get("counters") or {}
    if counters:
        scope = f" (since {'server' if stats.get('countersScope') == 'server' else 'database'} start)"
        lines.append("  Counters:    " + ", ".join(f"{k} {v}" for k, v in counters.items()) + scope)
    if stats.get("cacheHitRatio") is not None:
        lines.append(f"  Cache hits:  {stats['cacheHitRatio'] * 100:.1f}%")
    tables = stats.get("tables") or []
    if tables:
        lines.append("  Largest tables:")
        for t in tables:
            name = f"{t['schema']}.{t['name']}" if t.get("schema") else t["name"]
            rows = f"{t['rows']} rows" if t.get("rows") is not None else ""
            lines.append(f"    {name:<32} {format_bytes(t.get('bytes')) if t.get('bytes') is not None else '':>10}  {rows}")
    for u in stats.get("unavailable") or []:
        lines.append(f"  {u['metric']}: unavailable ({u['error']})")
    return "\n".join(lines)


def describe_databases(databases: list[dict[str, Any]]) -> str:
    """The databases list as text (terminal and MCP)."""
    if not databases:
        return "No databases registered."
    lines = []
    for d in databases:
        size = f", {format_bytes(d['sizeBytes'])}" if d.get("sizeBytes") is not None else ""
        if d.get("error"):
            lines.append(f"{d['name']} ({d['dialect']}{size}): {d['error']}")
        else:
            problems = f", {d['problems']} to look at" if d["problems"] else ""
            lines.append(f"{d['name']} ({d['dialect']}{size}): {d['applied']} applied, {d['pending']} pending{problems}")
    return "\n".join(lines)


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or (isinstance(value, str) and value.lower() in ("true", "1", ""))


def _caller() -> str | None:
    """The signed-in user running the command (``get_cli_caller``), when the server provides it."""
    try:
        from qodalis_cli import get_cli_caller  # the server package, optional here
    except ImportError:
        return None
    caller = get_cli_caller()
    return getattr(caller, "user_name", None) if caller is not None else None


class _ListProcessor(CliCommandProcessor):
    def __init__(self, databases: CliDatabases) -> None:
        self._databases = databases

    @property
    def command(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return "Lists the databases: dialect, size and migrations"

    @property
    def required_permissions(self) -> list[str]:
        return [DATABASES_READ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return describe_databases(await self._databases.overview())


class _StatsProcessor(CliCommandProcessor):
    def __init__(self, databases: CliDatabases) -> None:
        self._databases = databases

    @property
    def command(self) -> str:
        return "stats"

    @property
    def description(self) -> str:
        return "Shows a database's size, connections, load counters and largest tables"

    @property
    def required_permissions(self) -> list[str]:
        return [DATABASES_READ]

    @property
    def examples(self) -> list[CliCommandExample]:
        return [CliCommandExample("databases stats main", "Stats of the database main")]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            return describe_stats(await self._databases.stats(_database_of(self._databases, command)))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return f"Error: {exc}"


class _MigrationsProcessor(CliCommandProcessor):
    def __init__(self, databases: CliDatabases) -> None:
        self._databases = databases

    @property
    def command(self) -> str:
        return "migrations"

    @property
    def description(self) -> str:
        return "Shows the migrations of a database and their status"

    @property
    def required_permissions(self) -> list[str]:
        return [DATABASES_READ]

    @property
    def examples(self) -> list[CliCommandExample]:
        return [CliCommandExample("databases migrations main", "Migrations of the database main")]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            db = self._databases.require(_database_of(self._databases, command))
            states = await db.status()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return f"Error: {exc}"
        if not states:
            return f"{db.name}: no migrations"
        lines = [f"{db.name} ({db.dialect})"]
        lines += [
            f"  {s.status:<8} {s.source}:{s.id}  {s.name}{'  ' + s.applied_at if s.applied_at else ''}" for s in states
        ]
        return "\n".join(lines)


class _RunProcessor(CliCommandProcessor):
    def __init__(self, databases: CliDatabases, direction: str) -> None:
        self._databases = databases
        self._direction = direction

    @property
    def command(self) -> str:
        return self._direction

    @property
    def description(self) -> str:
        return (
            "Applies the pending migrations of a database" if self._direction == "up"
            else "Rolls back migrations of a database to --to"
        )

    @property
    def required_permissions(self) -> list[str]:
        return [DATABASES_MIGRATE]

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [_TO, _DRY_RUN, _FORCE]

    @property
    def examples(self) -> list[CliCommandExample]:
        if self._direction == "up":
            return [
                CliCommandExample("databases up main", "Apply every pending migration"),
                CliCommandExample("databases up main --dry-run", "Show the SQL first"),
            ]
        return [CliCommandExample("databases down main --to 0003", "Roll back what came after 0003")]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            name = _database_of(self._databases, command)
            raw_to: Any = (command.args or {}).get("to")
            to = str(raw_to) if raw_to not in (None, True, "") else None
            if self._direction == "down" and not to:
                return "Error: --to is required to roll back (an id, or base for everything)"
            if _flag(command, "dry-run"):
                db = self._databases.require(name)
                options = CliMigrationRunOptions(to=to, dry_run=True)
                result = await (db.up(options) if self._direction == "up" else db.down(options))
                return result["sql"] or "Nothing to run."  # type: ignore[index]
            run = await self._databases.run(name, self._direction, CliMigrationRunOptions(  # type: ignore[arg-type]
                to=to, force=_flag(command, "force"), actor=_caller(), trigger="terminal",
            ))
            return _describe_run(run)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return f"Error: {exc}"


class _RunsProcessor(CliCommandProcessor):
    def __init__(self, databases: CliDatabases) -> None:
        self._databases = databases

    command = "runs"
    description = "Shows the migration run history of a database: time, direction, status, count, who ran it, error"
    value_required = False
    required_permissions = [DATABASES_READ]
    parameters = [_LIMIT, _JSON]
    examples = [
        CliCommandExample("databases runs main", "The last 20 runs of the database main"),
        CliCommandExample("databases runs main -n 5 --json", "The last 5 runs as JSON"),
    ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            limit = limit_of(command, 20, 100)
            if isinstance(limit, str):
                return limit
            name = _database_of(self._databases, command)
            db = self._databases.database(name)
            if db is None:
                return f"Error: unknown database: {name}"
            items, total = await db.runs(limit, 0)
            if _flag(command, "json"):
                return json.dumps({"database": db.name, "items": [r.to_dict() for r in items], "total": total}, indent=2)
            if not items:
                return f"{db.name}: no migration runs yet"
            return "\n".join([
                f"{db.name} ({db.dialect}): {len(items)} of {total} run(s)",
                f"{'Started'.ljust(19)} Dir  Status    Count Trigger",
                *(format_run_line(r) for r in items),
            ])
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return f"Error: {exc}"


class CliDatabasesCommandProcessor(CliCommandProcessor):
    """``databases`` (alias ``db``): ``list``, ``stats <db>``, ``migrations <db>``, ``up <db>``,
    ``down <db> --to <id>``, ``runs <db>``."""

    def __init__(self, databases: CliDatabases) -> None:
        self._databases = databases
        self._processors: list[ICliCommandProcessor] = [
            _ListProcessor(databases),
            _StatsProcessor(databases),
            _MigrationsProcessor(databases),
            _RunProcessor(databases, "up"),
            _RunProcessor(databases, "down"),
            _RunsProcessor(databases),
        ]

    @property
    def command(self) -> str:
        return "databases"

    @property
    def aliases(self) -> list[str]:
        return ["db"]

    @property
    def description(self) -> str:
        return "Databases: stats and migrations (status, apply, roll back)"

    @property
    def module(self) -> str:
        return "databases"

    @property
    def icon(self) -> str:
        return "🗄️"

    @property
    def required_permissions(self) -> list[str]:
        return [DATABASES_READ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return describe_databases(await self._databases.overview())
