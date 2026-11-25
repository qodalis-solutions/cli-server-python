"""``CliDatabaseConfigStore``: writable settings in the ``QcliConfig`` table."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from qodalis_cli_server_abstractions import CliTableNameOptions, qcli_table_name
from qodalis_cli_server_abstractions.config import (
    CliConfigConflictError,
    CliConfigStoredSection,
    ICliConfigStore,
    apply_cli_config_changes,
)

from .connection import CliSqlConnection, CliSqlSession
from .dialects import TRANSACTION, quote_ident, quote_table, table_exists
from .runner import CliMigrationDatabaseOptions, CliMigrationRunner, CliMigrationRunOptions
from .sources import CliMigration, CodeSource, code_source

#: The migration source of the config table.
CONFIG_STORE_MIGRATION_SOURCE = "qcli-config"

_COLUMN_TYPES = {
    "postgres": {"key": "varchar(150)", "long": "text", "time": "timestamptz", "int": "integer"},
    "mysql": {"key": "varchar(150)", "long": "longtext", "time": "datetime(3)", "int": "int"},
    "mssql": {"key": "nvarchar(150)", "long": "nvarchar(max)", "time": "datetime2", "int": "int"},
    "sqlite": {"key": "TEXT", "long": "TEXT", "time": "TEXT", "int": "INTEGER"},
}


def config_store_migrations(table: str, schema: Optional[str] = None) -> CodeSource:
    """The config table's schema as migrations (source ``qcli-config``): one row per provider section."""

    async def config_table(db: CliSqlSession) -> None:
        if await table_exists(db, table, schema):
            return
        d = db.dialect
        t = _COLUMN_TYPES[d]

        def q(name: str) -> str:
            return quote_ident(d, name)

        columns = ", ".join([
            f"{q('Provider')} {t['key']} NOT NULL",
            f"{q('Section')} {t['key']} NOT NULL",
            f"{q('Version')} {t['int']} NOT NULL",
            f"{q('Data')} {t['long']} NOT NULL",
            f"{q('UpdatedAt')} {t['time']} NOT NULL",
            f"CONSTRAINT {q(f'PK_{table}')} PRIMARY KEY ({q('Provider')}, {q('Section')})",
        ])
        await db.exec(f"CREATE TABLE {quote_table(d, table, schema)} ({columns})")

    return code_source(CONFIG_STORE_MIGRATION_SOURCE, [CliMigration("1", "config table", config_table)])


def _parse(row: Optional[Mapping[str, Any]]) -> CliConfigStoredSection:
    if not row:
        return CliConfigStoredSection()
    try:
        parsed = json.loads(str(row.get("Data")))
    except ValueError:
        parsed = {}
    try:
        version = int(row.get("Version") or 0)
    except (TypeError, ValueError):
        version = 0
    return CliConfigStoredSection(parsed if isinstance(parsed, dict) else {}, version)


class CliDatabaseConfigStore(ICliConfigStore):
    """Keeps writable settings in a database table (``QcliConfig``, source
    ``database``): one row per provider section holding its values as JSON
    and its version.  A change runs in a transaction with the row locked
    (``FOR UPDATE`` on PostgreSQL and MySQL, ``UPDLOCK, HOLDLOCK`` on SQL
    Server, ``BEGIN IMMEDIATE`` on SQLite), so two servers sharing the
    database never lose a change and a stale version is refused.  The table
    is created by the migration source ``qcli-config`` before the first read
    or write (:meth:`ready`); add :attr:`migration_source` to the databases
    plugin to see it there::

        store = CliDatabaseConfigStore(postgres_connection(pool))
        create_cli_server(CliServerOptions(config=CliServerConfigOptions(store=store)))
    """

    source = "database"

    def __init__(
        self,
        connection: CliSqlConnection,
        table_prefix: Optional[str] = None,
        schema: Optional[str] = None,
        tables: Optional[dict[str, str]] = None,
    ) -> None:
        self.connection = connection
        self.schema = schema
        self.table = qcli_table_name(
            "Config", CliTableNameOptions(table_prefix=table_prefix, tables=dict(tables or {}), schema=schema),
        )
        #: The table's migrations, to show them in the databases plugin (``add_source``).
        self.migration_source = config_store_migrations(self.table, schema)
        self._runner = CliMigrationRunner(
            "config", CliMigrationDatabaseOptions(connection, table_prefix=table_prefix, schema=schema),
        ).add_source(self.migration_source)
        self._ready = False
        self._ready_lock: Optional[asyncio.Lock] = None

    async def ready(self) -> None:
        """Creates the table (once); every read and write waits for it."""
        if self._ready:
            return
        if self._ready_lock is None:
            self._ready_lock = asyncio.Lock()
        async with self._ready_lock:
            if self._ready:
                return
            run = await self._runner.up(CliMigrationRunOptions(trigger="startup", wait=True))
            if getattr(run, "status", None) == "failed":
                raise RuntimeError(f"config store schema: {getattr(run, 'error', '')}")
            self._ready = True

    def _sql(self, dialect: str) -> tuple[Any, str]:
        return (lambda name: quote_ident(dialect, name)), quote_table(dialect, self.table, self.schema)

    async def read(self, provider: str, section: str) -> CliConfigStoredSection:
        await self.ready()
        async with self.connection.session() as db:
            q, table = self._sql(db.dialect)
            rows = await db.query(
                f"SELECT {q('Version')} AS {q('Version')}, {q('Data')} AS {q('Data')} FROM {table} "
                f"WHERE {q('Provider')} = ? AND {q('Section')} = ?",
                [provider, section],
            )
            return _parse(rows[0] if rows else None)

    async def write(
        self,
        provider: str,
        section: str,
        changes: Mapping[str, Any],
        expected_version: Optional[int] = None,
    ) -> CliConfigStoredSection:
        await self.ready()
        # A first write of a section may race another server's insert (primary key): read again and retry once
        attempt = 0
        while True:
            try:
                async with self.connection.session() as db:
                    return await self._write_on(db, provider, section, changes, expected_version)
            except CliConfigConflictError:
                raise
            except Exception:
                if attempt > 0:
                    raise
                attempt += 1

    async def _write_on(
        self,
        db: CliSqlSession,
        provider: str,
        section: str,
        changes: Mapping[str, Any],
        expected_version: Optional[int],
    ) -> CliConfigStoredSection:
        d = db.dialect
        q, table = self._sql(d)
        tx = TRANSACTION[d]
        await db.exec(tx["begin"])
        try:
            lock = " FOR UPDATE" if d in ("postgres", "mysql") else ""
            hint = " WITH (UPDLOCK, HOLDLOCK)" if d == "mssql" else ""
            rows = await db.query(
                f"SELECT {q('Version')} AS {q('Version')}, {q('Data')} AS {q('Data')} FROM {table}{hint} "
                f"WHERE {q('Provider')} = ? AND {q('Section')} = ?{lock}",
                [provider, section],
            )
            current = _parse(rows[0] if rows else None)
            if expected_version is not None and expected_version != current.version:
                raise CliConfigConflictError(str(current.version))
            stored = CliConfigStoredSection(apply_cli_config_changes(current.values, changes), current.version + 1)
            data = json.dumps(stored.values, ensure_ascii=False)
            now = datetime.now(timezone.utc)
            if rows:
                await db.query(
                    f"UPDATE {table} SET {q('Version')} = ?, {q('Data')} = ?, {q('UpdatedAt')} = ? "
                    f"WHERE {q('Provider')} = ? AND {q('Section')} = ?",
                    [stored.version, data, now, provider, section],
                )
            else:
                await db.query(
                    f"INSERT INTO {table} ({q('Provider')}, {q('Section')}, {q('Version')}, {q('Data')}, "
                    f"{q('UpdatedAt')}) VALUES (?, ?, ?, ?, ?)",
                    [provider, section, stored.version, data, now],
                )
            await db.exec(tx["commit"])
            return stored
        except BaseException:
            try:
                await db.exec(tx["rollback"])
            except Exception:  # noqa: BLE001 - the original error wins
                pass
            raise
