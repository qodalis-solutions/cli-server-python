"""Runs one database's migrations: status, up, down, and the run history."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from qodalis_cli_server_abstractions import CliTableNameOptions, qcli_table_name

from .connection import CliSqlConnection, CliSqlSession
from .dialects import (
    TRANSACTION,
    HistoryTables,
    acquire_lock,
    ensure_history_tables,
    quote_ident,
    quote_table,
    release_lock,
    table_exists,
)
from .sources import CliMigration, CliMigrationSource, checksum_of, id_sort_key, step_sql

#: Where a migration stands in a database.
CliMigrationStatus = Literal["applied", "pending", "changed", "missing", "failed"]
#: What started a run.
CliMigrationTrigger = Literal["startup", "api", "terminal", "mcp"]


def iso(value: Any) -> str | None:
    """A time as ``2026-11-23T10:00:00.000Z`` (naive values are UTC)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    return str(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CliMigrationState:
    """A migration with its status (``GET /api/v1/qcli/databases/{db}/migrations``)."""

    source: str
    id: str
    name: str
    status: CliMigrationStatus
    has_down: bool
    applied_at: str | None = None
    duration_ms: int | None = None
    applied_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "id": self.id, "name": self.name, "status": self.status}
        if self.applied_at is not None:
            out["appliedAt"] = self.applied_at
        if self.duration_ms is not None:
            out["durationMs"] = self.duration_ms
        if self.applied_by:
            out["appliedBy"] = self.applied_by
        out["hasDown"] = self.has_down
        return out


@dataclass
class CliMigrationRun:
    """One invocation of up or down, kept in ``QcliMigrationRuns``."""

    id: str
    direction: Literal["up", "down"]
    trigger: CliMigrationTrigger
    started_at: str
    status: Literal["running", "succeeded", "failed"]
    target: str | None = None
    actor: str | None = None
    finished_at: str | None = None
    error: str | None = None
    #: The migrations it applied (up) or rolled back (down), in order, with their durations
    #: (``{"source", "id", "name", "durationMs"}``).
    applied: list[dict[str, Any]] = field(default_factory=list)
    #: The migration it stopped at (``{"source", "id"}``).
    failed: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "direction": self.direction}
        if self.target is not None:
            out["target"] = self.target
        out["trigger"] = self.trigger
        if self.actor:
            out["actor"] = self.actor
        out["startedAt"] = self.started_at
        if self.finished_at:
            out["finishedAt"] = self.finished_at
        out["status"] = self.status
        if self.error:
            out["error"] = self.error
        out["applied"] = list(self.applied)
        if self.failed:
            out["failed"] = dict(self.failed)
        return out


@dataclass
class CliMigrationRunOptions:
    #: Up: stop after this migration (``id`` or ``source:id``).  Down: roll back to it, keeping it
    #: (``base`` = everything).
    to: str | None = None
    #: Run although a migration is ``changed`` or ``missing``.
    force: bool = False
    #: Return the SQL instead of running it.
    dry_run: bool = False
    actor: str | None = None
    trigger: CliMigrationTrigger = "api"
    #: Wait for another process's run instead of answering busy (startup does).
    wait: bool = False


class CliMigrationError(Exception):
    """A refused request: ``status`` is the HTTP status the API answers (400, 404, 409)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class CliMigrationDatabaseOptions:
    """A database to migrate."""

    connection: CliSqlConnection
    #: Apply pending migrations when the server starts (it waits for them; a failure stops startup).
    apply_on_startup: bool = False
    #: Names of the history tables (``QcliMigrations``, ``QcliMigrationRuns``): prefix, schema, overrides.
    table_prefix: str | None = None
    schema: str | None = None
    tables: dict[str, str] = field(default_factory=dict)


@dataclass
class _Loaded:
    source: str
    migration: CliMigration
    checksum: str | None


def _key(source: str, mid: str) -> str:
    return f"{source}\u0000{mid}"


class CliMigrationRunner:
    """Runs one database's migrations: status, up, down, and the run history."""

    def __init__(self, name: str, options: CliMigrationDatabaseOptions) -> None:
        self.name = name
        self.options = options
        names = CliTableNameOptions(table_prefix=options.table_prefix, tables=dict(options.tables or {}), schema=options.schema)
        self.tables = HistoryTables(
            migrations=qcli_table_name("Migrations", names),
            runs=qcli_table_name("MigrationRuns", names),
        )
        self._sources: list[CliMigrationSource] = []
        self._running: asyncio.Future[Any] | None = None
        self._current_run: str | None = None

    @property
    def dialect(self) -> str:
        return self.options.connection.dialect

    def add_source(self, source: CliMigrationSource) -> "CliMigrationRunner":
        """Adds a source; sources apply in the order they were added."""
        if any(s.name == source.name for s in self._sources):
            raise ValueError(f"migration source already added to {self.name}: {source.name}")
        self._sources.append(source)
        return self

    @property
    def source_names(self) -> list[str]:
        return [s.name for s in self._sources]

    async def _load(self) -> list[_Loaded]:
        out: list[_Loaded] = []
        for source in self._sources:
            migrations = sorted(await source.load(), key=lambda m: id_sort_key(m.id))
            for a, b in zip(migrations, migrations[1:]):
                if a.id == b.id:
                    raise ValueError(f"duplicate migration {source.name}:{b.id}")
            for m in migrations:
                out.append(_Loaded(source.name, m, checksum_of(m, self.dialect)))
        return out

    def _table(self, name: str) -> str:
        return quote_table(self.dialect, name, self.options.schema)

    def _cols(self, *names: str) -> str:
        return ", ".join(quote_ident(self.dialect, n) for n in names)

    def _c(self, name: str) -> str:
        return quote_ident(self.dialect, name)

    async def _history(self, db: CliSqlSession) -> list[dict[str, Any]]:
        if not await table_exists(db, self.tables.migrations, self.options.schema):
            return []
        return await db.query(
            f"SELECT {self._cols('Source', 'Id', 'Name', 'Checksum', 'AppliedAt', 'DurationMs', 'AppliedBy')} "
            f"FROM {self._table(self.tables.migrations)}"
        )

    async def _last_failure(self, db: CliSqlSession) -> dict[str, str] | None:
        items, _ = await self._runs_on(db, 1, 0)
        return items[0].failed if items and items[0].status == "failed" else None

    def _states_of(
        self, loaded: list[_Loaded], rows: list[dict[str, Any]], failed: dict[str, str] | None = None,
    ) -> list[CliMigrationState]:
        applied = {_key(r["Source"], str(r["Id"])): r for r in rows}
        known = {_key(l.source, l.migration.id) for l in loaded}
        states: list[CliMigrationState] = []
        for l in loaded:
            row = applied.get(_key(l.source, l.migration.id))
            has_down = l.migration.down is not None
            if row is None:
                is_failed = failed is not None and failed.get("source") == l.source and failed.get("id") == l.migration.id
                states.append(CliMigrationState(l.source, l.migration.id, l.migration.name, "failed" if is_failed else "pending", has_down))
                continue
            changed = bool(row["Checksum"]) and bool(l.checksum) and row["Checksum"] != l.checksum
            states.append(CliMigrationState(
                l.source, l.migration.id, l.migration.name, "changed" if changed else "applied", has_down,
                applied_at=iso(row["AppliedAt"]), duration_ms=int(row["DurationMs"]), applied_by=row["AppliedBy"] or None,
            ))
        # Applied, but gone from its source (a source this runner still has)
        sources = set(self.source_names)
        for row in rows:
            if row["Source"] in sources and _key(row["Source"], str(row["Id"])) not in known:
                states.append(CliMigrationState(
                    row["Source"], str(row["Id"]), row["Name"], "missing", False,
                    applied_at=iso(row["AppliedAt"]), duration_ms=int(row["DurationMs"]), applied_by=row["AppliedBy"] or None,
                ))
        return states

    async def status(self) -> list[CliMigrationState]:
        """Every migration with its status."""
        loaded = await self._load()
        async with self.options.connection.session() as db:
            return self._states_of(loaded, await self._history(db), await self._last_failure(db))

    async def runs(self, limit: int = 20, offset: int = 0) -> tuple[list[CliMigrationRun], int]:
        """The latest runs, newest first, and their total."""
        async with self.options.connection.session() as db:
            return await self._runs_on(db, limit, offset)

    async def _runs_on(self, db: CliSqlSession, limit: int, offset: int) -> tuple[list[CliMigrationRun], int]:
        if not await table_exists(db, self.tables.runs, self.options.schema):
            return [], 0
        table = self._table(self.tables.runs)
        total_rows = await db.query(f"SELECT COUNT(*) AS n FROM {table}")
        total = int(total_rows[0]["n"]) if total_rows else 0
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        page = (
            f"OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY" if self.dialect == "mssql"
            else f"LIMIT {limit} OFFSET {offset}"
        )
        rows = await db.query(
            f"SELECT {self._cols('Id', 'Direction', 'Target', 'Trigger', 'Actor', 'StartedAt', 'FinishedAt', 'Status', 'Error', 'Applied')} "
            f"FROM {table} ORDER BY {self._c('StartedAt')} DESC, {self._c('Id')} DESC {page}"
        )
        return [self._run_of(r) for r in rows], total

    @staticmethod
    def _run_of(r: dict[str, Any]) -> CliMigrationRun:
        detail = json.loads(r["Applied"]) if r.get("Applied") else {"applied": []}
        return CliMigrationRun(
            id=str(r["Id"]),
            direction=r["Direction"],
            trigger=r["Trigger"],
            started_at=iso(r["StartedAt"]) or "",
            status=r["Status"],
            target=str(r["Target"]) if r.get("Target") else None,
            actor=str(r["Actor"]) if r.get("Actor") else None,
            finished_at=iso(r.get("FinishedAt")),
            error=str(r["Error"]) if r.get("Error") else None,
            applied=detail.get("applied") or [],
            failed=detail.get("failed") or None,
        )

    def _find(self, loaded: list[_Loaded], target: str) -> int:
        """A migration's index by ``id`` or ``source:id``."""
        qualified = target.find(":") > 0
        matches = [
            i for i, l in enumerate(loaded)
            if (f"{l.source}:{l.migration.id}" if qualified else l.migration.id) == target
        ]
        if not matches:
            raise CliMigrationError(f"unknown migration: {target}", 404)
        if len(matches) > 1:
            raise CliMigrationError(f"migration {target} is in several sources: name it as source:id", 400)
        return matches[0]

    async def up(self, options: CliMigrationRunOptions | None = None) -> CliMigrationRun | dict[str, str]:
        """Applies pending migrations (up to ``to``).  Dry run: ``{"sql": ...}``, what it would run."""
        opts = options or CliMigrationRunOptions()
        return await self._exclusive(opts, lambda db, loaded: self._run_up(db, loaded, opts))

    async def down(self, options: CliMigrationRunOptions) -> CliMigrationRun | dict[str, str]:
        """Rolls back applied migrations, newest first, down to ``to`` (kept; ``base`` = everything)."""
        if not options.to:
            raise CliMigrationError("to is required to roll back (an id, or base for everything)", 400)
        return await self._exclusive(options, lambda db, loaded: self._run_down(db, loaded, options))

    async def _exclusive(
        self,
        options: CliMigrationRunOptions,
        fn: Callable[[CliSqlSession, list[_Loaded]], Awaitable[Any]],
    ) -> Any:
        """Runs ``fn`` alone: one run per process at a time, and the database lock across processes."""
        if options.dry_run:
            loaded = await self._load()
            async with self.options.connection.session() as db:
                return await fn(db, loaded)
        while self._running is not None:
            if not options.wait:
                raise CliMigrationError(f"a migration run is in progress on {self.name}", 409)
            try:
                await asyncio.shield(self._running)
            except Exception:
                pass

        async def run() -> Any:
            loaded = await self._load()
            async with self.options.connection.session() as db:
                key = f"qcli-migrations:{self.options.schema or ''}.{self.tables.migrations}"
                if not await acquire_lock(db, key, options.wait):
                    raise CliMigrationError(f"a migration run is in progress on {self.name}", 409)
                try:
                    await ensure_history_tables(db, self.tables, self.options.schema)
                    return await fn(db, loaded)
                finally:
                    try:
                        await release_lock(db, key)
                    except Exception:
                        pass

        task = asyncio.ensure_future(run())
        self._running = task
        try:
            return await task
        finally:
            self._running = None

    @staticmethod
    def _blocked(states: list[CliMigrationState], force: bool) -> None:
        problems = [s for s in states if s.status in ("changed", "missing")]
        if problems and not force:
            listed = ", ".join(f"{p.source}:{p.id} ({p.status})" for p in problems)
            raise CliMigrationError(
                f"migrations changed or missing since they were applied: {listed}; fix them or run with force", 409,
            )

    async def _run_up(self, db: CliSqlSession, loaded: list[_Loaded], options: CliMigrationRunOptions) -> Any:
        states = self._states_of(loaded, await self._history(db))
        self._blocked(states, options.force)
        last = self._find(loaded, options.to) if options.to is not None else len(loaded) - 1
        pending = [l for i, l in enumerate(loaded) if i <= last and states[i].status not in ("applied", "changed")]
        if options.dry_run:
            return {"sql": self._script(pending, "up")}

        async def step(l: _Loaded) -> int:
            started = time.monotonic()

            async def bookkeeping(duration_ms: int) -> None:
                await db.query(
                    f"INSERT INTO {self._table(self.tables.migrations)} "
                    f"({self._cols('Source', 'Id', 'Name', 'Checksum', 'AppliedAt', 'DurationMs', 'AppliedBy', 'RunId')}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [l.source, l.migration.id, l.migration.name, l.checksum, _now(), duration_ms, options.actor, self._current_run],
                )

            await self._apply(db, l, "up", bookkeeping)
            return int((time.monotonic() - started) * 1000)

        return await self._record(db, "up", options, pending, step)

    async def _run_down(self, db: CliSqlSession, loaded: list[_Loaded], options: CliMigrationRunOptions) -> Any:
        states = self._states_of(loaded, await self._history(db))
        keep = -1 if options.to == "base" else self._find(loaded, options.to or "")
        undo = [l for i, l in enumerate(loaded) if i > keep and states[i].status in ("applied", "changed")]
        undo.reverse()
        no_down = [l for l in undo if l.migration.down is None]
        if no_down:
            listed = ", ".join(f"{l.source}:{l.migration.id}" for l in no_down)
            raise CliMigrationError(f"cannot roll back: no down step for {listed}", 409)
        if options.dry_run:
            return {"sql": self._script(undo, "down")}

        async def step(l: _Loaded) -> int:
            started = time.monotonic()

            async def bookkeeping(_: int) -> None:
                await db.query(
                    f"DELETE FROM {self._table(self.tables.migrations)} WHERE {self._c('Source')} = ? AND {self._c('Id')} = ?",
                    [l.source, l.migration.id],
                )

            await self._apply(db, l, "down", bookkeeping)
            return int((time.monotonic() - started) * 1000)

        return await self._record(db, "down", options, undo, step)

    def _script(self, items: list[_Loaded], direction: str) -> str:
        """The SQL of the steps, for a dry run (code steps show as a comment)."""
        parts = []
        for l in items:
            step = l.migration.up if direction == "up" else l.migration.down
            sql = step_sql(step, self.dialect, l.migration.id)  # type: ignore[arg-type]
            parts.append(
                f"-- {l.source}:{l.migration.id} {l.migration.name} ({direction})\n"
                + (sql if sql is not None else "-- (code: runs a function, no SQL to show)")
            )
        return "\n\n".join(parts)

    async def _apply(
        self, db: CliSqlSession, l: _Loaded, direction: str, bookkeeping: Callable[[int], Awaitable[None]],
    ) -> None:
        """Runs the step and its history change, in one transaction unless the migration opts out."""
        step = l.migration.up if direction == "up" else l.migration.down
        tx = TRANSACTION[self.dialect]
        in_transaction = l.migration.transaction is not False
        started = time.monotonic()
        if in_transaction:
            await db.exec(tx["begin"])
        try:
            sql = step_sql(step, self.dialect, l.migration.id)  # type: ignore[arg-type]
            if sql is None:
                await step(db)  # type: ignore[misc,operator]
            elif sql.strip():
                await db.exec(sql)
            await bookkeeping(int((time.monotonic() - started) * 1000))
            if in_transaction:
                await db.exec(tx["commit"])
        except BaseException:
            if in_transaction:
                try:
                    await db.exec(tx["rollback"])
                except Exception:
                    pass
            raise

    async def _record(
        self,
        db: CliSqlSession,
        direction: Literal["up", "down"],
        options: CliMigrationRunOptions,
        items: list[_Loaded],
        step: Callable[[_Loaded], Awaitable[int]],
    ) -> CliMigrationRun:
        """Runs the steps, recording the run before and at the end."""
        started = _now()
        run = CliMigrationRun(
            id=str(uuid.uuid4()),
            direction=direction,
            trigger=options.trigger,
            started_at=iso(started) or "",
            status="running",
            target=options.to,
            actor=options.actor or None,
        )
        await db.query(
            f"INSERT INTO {self._table(self.tables.runs)} "
            f"({self._cols('Id', 'Direction', 'Target', 'Trigger', 'Actor', 'StartedAt', 'Status', 'Applied')}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [run.id, direction, run.target, run.trigger, run.actor, started, "running", json.dumps({"applied": []})],
        )
        self._current_run = run.id
        try:
            for l in items:
                try:
                    duration_ms = await step(l)
                    run.applied.append({"source": l.source, "id": l.migration.id, "name": l.migration.name, "durationMs": duration_ms})
                except Exception as exc:  # noqa: BLE001 - recorded on the run
                    run.status = "failed"
                    run.error = f"{l.source}:{l.migration.id} {l.migration.name}: {exc}"
                    run.failed = {"source": l.source, "id": l.migration.id}
                    break
        finally:
            self._current_run = None
        if run.status == "running":
            run.status = "succeeded"
        finished = _now()
        run.finished_at = iso(finished)
        detail: dict[str, Any] = {"applied": run.applied}
        if run.failed:
            detail["failed"] = run.failed
        await db.query(
            f"UPDATE {self._table(self.tables.runs)} SET {self._c('FinishedAt')} = ?, {self._c('Status')} = ?, "
            f"{self._c('Error')} = ?, {self._c('Applied')} = ? WHERE {self._c('Id')} = ?",
            [finished, run.status, run.error, json.dumps(detail), run.id],
        )
        return run
