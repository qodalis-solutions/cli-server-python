"""Terminal commands: ``migrations`` (the databases), ``migrations status|up|down <db>``."""

from __future__ import annotations

import asyncio
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

from .migrations import MIGRATIONS_READ, MIGRATIONS_RUN, CliMigrations
from .runner import CliMigrationError, CliMigrationRun, CliMigrationRunOptions

_TO = CliCommandParameterDescriptor(
    "to", "Migration to stop at (id or source:id; down: base for everything)", False, "string",
)
_DRY_RUN = CliCommandParameterDescriptor("dry-run", "Show the SQL instead of running it", False, "boolean")
_FORCE = CliCommandParameterDescriptor("force", "Run although migrations changed or went missing", False, "boolean")


def _database_of(migrations: CliMigrations, command: CliProcessCommand) -> str:
    name = get_positional_text(command) or (migrations.databases[0].name if len(migrations.databases) == 1 else "")
    if not name:
        raise CliMigrationError(f"name the database: {', '.join(d.name for d in migrations.databases)}", 400)
    return name


def _describe_run(run: CliMigrationRun) -> str:
    verb = "Applied" if run.direction == "up" else "Rolled back"
    lines = [f"{verb} {len(run.applied)} migration(s) ({run.status})"]
    lines += [f"  {a['source']}:{a['id']}  {a['name']}  {a['durationMs']} ms" for a in run.applied]
    if run.error:
        lines.append(f"Failed: {run.error}")
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


class _StatusProcessor(CliCommandProcessor):
    def __init__(self, migrations: CliMigrations) -> None:
        self._migrations = migrations

    @property
    def command(self) -> str:
        return "status"

    @property
    def description(self) -> str:
        return "Shows the migrations of a database and their status"

    @property
    def required_permissions(self) -> list[str]:
        return [MIGRATIONS_READ]

    @property
    def examples(self) -> list[CliCommandExample]:
        return [CliCommandExample("migrations status main", "Migrations of the database main")]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            db = self._migrations.require(_database_of(self._migrations, command))
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
    def __init__(self, migrations: CliMigrations, direction: str) -> None:
        self._migrations = migrations
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
        return [MIGRATIONS_RUN]

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [_TO, _DRY_RUN, _FORCE]

    @property
    def examples(self) -> list[CliCommandExample]:
        if self._direction == "up":
            return [
                CliCommandExample("migrations up main", "Apply every pending migration"),
                CliCommandExample("migrations up main --dry-run", "Show the SQL first"),
            ]
        return [CliCommandExample("migrations down main --to 0003", "Roll back what came after 0003")]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        try:
            name = _database_of(self._migrations, command)
            raw_to: Any = (command.args or {}).get("to")
            to = str(raw_to) if raw_to not in (None, True, "") else None
            if self._direction == "down" and not to:
                return "Error: --to is required to roll back (an id, or base for everything)"
            if _flag(command, "dry-run"):
                db = self._migrations.require(name)
                options = CliMigrationRunOptions(to=to, dry_run=True)
                result = await (db.up(options) if self._direction == "up" else db.down(options))
                return result["sql"] or "Nothing to run."  # type: ignore[index]
            run = await self._migrations.run(name, self._direction, CliMigrationRunOptions(  # type: ignore[arg-type]
                to=to, force=_flag(command, "force"), actor=_caller(), trigger="terminal",
            ))
            return _describe_run(run)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return f"Error: {exc}"


class CliMigrationsCommandProcessor(CliCommandProcessor):
    """``migrations`` (the databases), ``migrations status|up|down <db>``."""

    def __init__(self, migrations: CliMigrations) -> None:
        self._migrations = migrations
        self._processors: list[ICliCommandProcessor] = [
            _StatusProcessor(migrations), _RunProcessor(migrations, "up"), _RunProcessor(migrations, "down"),
        ]

    @property
    def command(self) -> str:
        return "migrations"

    @property
    def description(self) -> str:
        return "Database migrations: status, apply, roll back"

    @property
    def module(self) -> str:
        return "migrations"

    @property
    def icon(self) -> str:
        return "🗄️"

    @property
    def required_permissions(self) -> list[str]:
        return [MIGRATIONS_READ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        databases = await self._migrations.overview()
        if not databases:
            return "No databases registered."
        lines = []
        for d in databases:
            if d.get("error"):
                lines.append(f"{d['name']} ({d['dialect']}): {d['error']}")
            else:
                problems = f", {d['problems']} to look at" if d["problems"] else ""
                lines.append(f"{d['name']} ({d['dialect']}): {d['applied']} applied, {d['pending']} pending{problems}")
        return "\n".join(lines)
