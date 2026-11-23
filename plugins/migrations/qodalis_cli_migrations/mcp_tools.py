"""MCP tools of the migrations plugin (``migrations_*``), registered on the CLI server's MCP tool registry when
the plugin is mounted.  Imported lazily (requires ``qodalis_cli``)."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from qodalis_cli.mcp import McpCallContext, McpTool, McpToolResult, object_schema

from .controller import TO_REQUIRED, caller_name
from .migrations import MIGRATIONS_READ, MIGRATIONS_RUN, CliMigrations
from .runner import CliMigrationRunOptions

READ_ONLY = {"readOnlyHint": True}
DESTRUCTIVE = {"destructiveHint": True}

_DATABASE = {"type": "string", "description": "Database name (see migrations_status without one)"}
_FORCE = {"type": "boolean", "description": "Run although a migration is changed or missing"}

Handler = Callable[[dict[str, Any], McpCallContext], Awaitable[McpToolResult]]


def _guarded(fn: Handler) -> Handler:
    async def call(args: dict[str, Any], context: McpCallContext) -> McpToolResult:
        try:
            return await fn(args, context)
        except Exception as exc:  # noqa: BLE001 - the tool's error
            return McpToolResult.error(getattr(exc, "message", None) or str(exc))

    return call


def _optional(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class MigrationsMcpToolProvider:
    """``migrations_status``, ``migrations_runs``, ``migrations_sql`` (``migrations:read``),
    ``migrations_up`` and ``migrations_down`` (``migrations:run``)."""

    name = "migrations"

    def __init__(self, migrations: CliMigrations) -> None:
        self.migrations = migrations

    def get_tools(self, context: McpCallContext) -> list[McpTool]:
        m = self.migrations

        async def status(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            if not _optional(args.get("database")):
                databases = await m.overview()
                lines = []
                for d in databases:
                    if d.get("error"):
                        lines.append(f"{d['name']} ({d['dialect']}): error: {d['error']}")
                    else:
                        problems = f", {d['problems']} problem(s)" if d["problems"] else ""
                        lines.append(f"{d['name']} ({d['dialect']}): {d['applied']} applied, {d['pending']} pending{problems}")
                return McpToolResult(text="\n".join(lines) or "No databases", structured={"databases": databases})
            db = m.require(args["database"])
            states = [s.to_dict() for s in await db.status()]
            text = "\n".join(f"{s['status']:<8} {s['source']}:{s['id']} {s['name']}" for s in states)
            return McpToolResult(
                text=text or "No migrations",
                structured={"name": db.name, "dialect": db.dialect, "sources": db.source_names, "migrations": states},
            )

        async def runs(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            try:
                limit = int(args.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            limit = min(max(limit, 1), 100)
            items, total = await m.require(args["database"]).runs(limit, 0)
            value = {"items": [r.to_dict() for r in items], "total": total}
            return McpToolResult(text=json.dumps(value, indent=2, ensure_ascii=False), structured=value)

        async def sql(args: dict[str, Any], _: McpCallContext) -> McpToolResult:
            db = m.require(args["database"])
            to = _optional(args.get("to"))
            if args.get("direction") == "down":
                if not to:
                    return McpToolResult.error(TO_REQUIRED)
                result = await db.down(CliMigrationRunOptions(to=to, dry_run=True))
                return McpToolResult(text=result["sql"] or "Nothing to roll back", structured=result)  # type: ignore[index,arg-type]
            result = await db.up(CliMigrationRunOptions(to=to, dry_run=True))
            return McpToolResult(text=result["sql"] or "Nothing pending", structured=result)  # type: ignore[index,arg-type]

        def run(direction: str) -> Handler:
            async def call(args: dict[str, Any], ctx: McpCallContext) -> McpToolResult:
                options = CliMigrationRunOptions(
                    to=_optional(args.get("to")), force=args.get("force") is True, actor=caller_name(ctx.auth), trigger="mcp",
                )
                if direction == "down" and not options.to:
                    return McpToolResult.error(TO_REQUIRED)
                result = await m.run(m.require(args["database"]).name, direction, options)  # type: ignore[arg-type]
                verb = "Applied" if direction == "up" else "Rolled back"
                text = f"{verb} {len(result.applied)} migration(s) ({result.status})" + (f": {result.error}" if result.error else "")
                return McpToolResult(text=text, structured=result.to_dict(), is_error=result.status == "failed")

            return call

        return [
            McpTool(
                "migrations_status",
                "Without a database: every database with its applied, pending and problem counts. With one: its "
                "migrations and their status (applied, pending, changed, missing, failed)",
                object_schema({"database": _DATABASE}),
                _guarded(status), [MIGRATIONS_READ], dict(READ_ONLY),
            ),
            McpTool(
                "migrations_runs",
                "Run history of a database, newest first: direction, trigger, who, status, error and what each run applied",
                object_schema({
                    "database": _DATABASE,
                    "limit": {"type": "integer", "default": 20, "description": "Maximum runs (1-100)"},
                }, ["database"]),
                _guarded(runs), [MIGRATIONS_READ], dict(READ_ONLY),
            ),
            McpTool(
                "migrations_sql",
                "The SQL a run would execute, without running it (dry run)",
                object_schema({
                    "database": _DATABASE,
                    "direction": {"type": "string", "description": "up (default) or down"},
                    "to": {"type": "string", "description": "As for migrations_up / migrations_down"},
                }, ["database"]),
                _guarded(sql), [MIGRATIONS_READ], dict(READ_ONLY),
            ),
            McpTool(
                "migrations_up",
                "Apply the pending migrations of a database (all, or up to one), under its lock; refused while a "
                "migration is changed or missing unless force",
                object_schema({
                    "database": _DATABASE,
                    "to": {"type": "string", "description": "Stop after this migration (id or source:id); omit for all pending"},
                    "force": _FORCE,
                }, ["database"]),
                _guarded(run("up")), [MIGRATIONS_RUN],
            ),
            McpTool(
                "migrations_down",
                "Roll back the migrations of a database applied after a migration (to), running their down scripts",
                object_schema({
                    "database": _DATABASE,
                    "to": {"type": "string", "description": "Roll back to this migration, keeping it (id or source:id), or base for everything"},
                    "force": _FORCE,
                }, ["database", "to"]),
                _guarded(run("down")), [MIGRATIONS_RUN], dict(DESTRUCTIVE),
            ),
        ]
