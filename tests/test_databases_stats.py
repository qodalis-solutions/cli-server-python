"""Database stats (size, connections, counters, largest tables) on SQLite always, and on PostgreSQL, MySQL and SQL
Server when the ``QCLI_TEST_*`` variables are set (see ``migrations_targets``)."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from qodalis_cli_databases import (
    CliDatabases,
    CliMigrationDatabaseOptions,
    CliMigrationRunner,
    collect_size,
    collect_stats,
    sqlite_connection,
)
from tests.migrations_targets import TARGETS, create_table, drop_prefixed, q, unique


@pytest.fixture(params=[name for name, _ in TARGETS])
async def target(request):
    open_target = dict(TARGETS)[request.param]
    connection, close = await open_target()
    prefix = unique()
    table = f"{prefix}Notes"
    async with connection.session() as db:
        await db.exec(create_table(table, ', "Title" varchar(100)')[db.dialect])
        for i in range(5):
            await db.query(f"INSERT INTO {q(db.dialect, table)} ({q(db.dialect, 'Id')}, {q(db.dialect, 'Title')}) VALUES (?, ?)", [i, f"note {i}"])
    try:
        yield connection, table
    finally:
        await drop_prefixed(connection, prefix)
        await close()


async def test_collects_the_stats_of_each_dialect(target):
    connection, table = target
    stats = await collect_stats("main", connection)
    dialect = connection.dialect
    assert (stats["name"], stats["dialect"]) == ("main", dialect)
    assert stats["collectedAt"].endswith("Z")
    assert isinstance(stats["unavailable"], list)
    unavailable = {u["metric"] for u in stats["unavailable"]}

    assert stats["server"]["version"].split(" ")[0] in ("SQLite", "PostgreSQL", "MySQL", "MariaDB", "SQL")
    assert stats["size"]["bytes"] > 0
    names = [t["name"] for t in stats["tables"]]
    assert table in names or len(names) == 10
    if table in names:
        mine = next(t for t in stats["tables"] if t["name"] == table)
        # Row counts are estimates on PostgreSQL / MySQL until the table is analyzed
        assert dialect not in ("sqlite", "mssql") or mine["rows"] == 5

    if dialect == "sqlite":
        assert not {"connections", "counters", "cacheHitRatio", "countersScope"} & stats.keys()
        assert stats["unavailable"] == []
    else:
        assert stats["server"].get("uptimeSeconds", 0) >= 0
        if "connections" not in unavailable:
            assert stats["connections"]["open"] >= 1 and stats["connections"]["max"] > 0
        if "counters" not in unavailable:
            assert stats["countersScope"] == ("database" if dialect == "postgres" else "server")
            assert stats["counters"].get("transactions", stats["counters"].get("queries", 0)) >= 0
        if "cacheHitRatio" in stats:
            assert 0 <= stats["cacheHitRatio"] <= 1


async def test_size_is_best_effort(target):
    connection, _ = target
    assert (await collect_size(connection) or 0) > 0


class _Broken:
    dialect = "postgres"

    def session(self) -> Any:
        raise ConnectionRefusedError("connection refused")


async def test_a_database_that_is_down_answers_unavailable_not_an_error():
    stats = await collect_stats("down", _Broken())  # type: ignore[arg-type]
    assert stats["unavailable"] == [{"metric": "connection", "error": "connection refused"}]
    assert await collect_size(_Broken()) is None  # type: ignore[arg-type]

    databases = CliDatabases()
    databases.add(CliMigrationRunner("down", CliMigrationDatabaseOptions(_Broken())))  # type: ignore[arg-type]
    summary = (await databases.overview())[0]
    assert "sizeBytes" not in summary and summary["error"]


class _FailingSession:
    dialect = "sqlite"

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def query(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        if "sqlite_master" in sql:
            raise PermissionError("not authorized")
        return await self._inner.query(sql, params)

    async def exec(self, script: str) -> None:
        await self._inner.exec(script)


async def test_a_failing_metric_is_listed_and_the_rest_still_shows():
    inner = sqlite_connection(sqlite3.connect(":memory:"))

    class _Connection:
        dialect = "sqlite"

        def session(self) -> Any:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def wrapped():
                async with inner.session() as db:
                    yield _FailingSession(db)

            return wrapped()

    stats = await collect_stats("main", _Connection())  # type: ignore[arg-type]
    assert stats["unavailable"] == [{"metric": "tables", "error": "not authorized"}]
    assert stats["server"]["version"].startswith("SQLite ") and "size" in stats
