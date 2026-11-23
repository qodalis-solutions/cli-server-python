"""The registered databases and what happens around a run (event, audit entry)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from qodalis_cli_server_abstractions import AuditEntry, CliEventAudience, IAuditStore, ICliEventBus

from .runner import CliMigrationError, CliMigrationRun, CliMigrationRunner, CliMigrationRunOptions

logger = logging.getLogger(__name__)

#: The event published after every run (audience ``migrations:read``).
MIGRATION_RUN_EVENT = "migrations:run"

MIGRATIONS_READ = "migrations:read"
MIGRATIONS_RUN = "migrations:run"


class CliMigrations:
    """The registered databases: overview, and runs with their event and audit entry."""

    def __init__(self) -> None:
        self._runners: dict[str, CliMigrationRunner] = {}
        self.event_bus: ICliEventBus | None = None
        self.audit_store: IAuditStore | None = None

    def add(self, runner: CliMigrationRunner) -> None:
        if runner.name in self._runners:
            raise ValueError(f"migration database already added: {runner.name}")
        self._runners[runner.name] = runner

    def database(self, name: str) -> CliMigrationRunner | None:
        return self._runners.get(name)

    def require(self, name: str) -> CliMigrationRunner:
        runner = self._runners.get(name)
        if runner is None:
            raise CliMigrationError(f"unknown database: {name}", 404)
        return runner

    @property
    def databases(self) -> list[CliMigrationRunner]:
        return list(self._runners.values())

    async def _summary(self, db: CliMigrationRunner) -> dict[str, Any]:
        base: dict[str, Any] = {"name": db.name, "dialect": db.dialect, "sources": db.source_names}
        try:
            states = await db.status()
            runs, _ = await db.runs(1, 0)
        except Exception as exc:  # noqa: BLE001 - shown on the database
            return {**base, "applied": 0, "pending": 0, "problems": 0, "error": str(exc)}
        summary = {
            **base,
            "applied": sum(1 for s in states if s.status == "applied"),
            "pending": sum(1 for s in states if s.status in ("pending", "failed")),
            "problems": sum(1 for s in states if s.status in ("changed", "missing", "failed")),
        }
        if runs:
            last = runs[0].to_dict()
            summary["lastRun"] = {
                k: last[k] for k in ("id", "direction", "status", "startedAt", "trigger", "finishedAt", "actor", "error")
                if k in last
            }
        return summary

    async def overview(self) -> list[dict[str, Any]]:
        """Every database: applied, pending, problems and the last run (``GET /api/v1/qcli/migrations``)."""
        return list(await asyncio.gather(*(self._summary(db) for db in self.databases)))

    async def run(self, name: str, direction: Literal["up", "down"], options: CliMigrationRunOptions) -> CliMigrationRun:
        """Runs up or down on a database, then publishes the event and writes the audit entry."""
        db = self.require(name)
        result = await (db.up(options) if direction == "up" else db.down(options))
        assert isinstance(result, CliMigrationRun)
        run = result
        summary = ", ".join(f"{a['source']}:{a['id']}" for a in run.applied) or "nothing to do"
        if self.event_bus is not None:
            data: dict[str, Any] = {
                "database": name, "run": run.id, "direction": direction, "status": run.status, "count": len(run.applied),
            }
            if run.error:
                data["error"] = run.error
            await self.event_bus.publish(MIGRATION_RUN_EVENT, data, CliEventAudience(permission=MIGRATIONS_READ))
        if self.audit_store is not None:
            try:
                await self.audit_store.log(AuditEntry(
                    event=f"migrations.{direction}",
                    username=run.actor or run.trigger,
                    actor=run.actor,
                    target=f"database:{name}",
                    details=f"{summary}; failed: {run.error}" if run.status == "failed" else summary,
                    properties={"run": run.id, "status": run.status, "trigger": run.trigger, "applied": len(run.applied)},
                ))
            except Exception:  # noqa: BLE001 - the run happened; the audit log is best effort
                logger.exception("could not audit the migration run %s", run.id)
        return run
