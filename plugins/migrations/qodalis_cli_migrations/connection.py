"""Database connections the migrations plugin runs on: a session holds one connection for a while (a lock, a
transaction) and runs every statement on it, in order.  Adapters wrap the usual drivers; the plugin needs none
of them installed."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Literal, Protocol, Sequence

CliSqlDialect = Literal["postgres", "mysql", "mssql", "sqlite"]

SQL_DIALECTS: tuple[str, ...] = ("postgres", "mysql", "mssql", "sqlite")

Row = dict[str, Any]


class CliSqlSession(Protocol):
    """One connection held for a while: every statement runs on it, in order."""

    dialect: str

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """One statement with positional parameters written ``?`` (converted to the driver's style); its
        rows as dicts."""
        ...

    async def exec(self, script: str) -> None:
        """A script of one or more statements, without parameters (a migration's SQL)."""
        ...


class CliSqlConnection(Protocol):
    """A database the plugin migrates: ``async with connection.session() as db``."""

    dialect: str

    def session(self) -> Any:
        """An async context manager giving a :class:`CliSqlSession` on a dedicated connection."""
        ...


def number_placeholders(sql: str, style: Callable[[int], str]) -> str:
    """``?`` placeholders (outside quotes) as ``$1, $2`` (PostgreSQL) or ``%s`` (MySQL, SQL Server)."""
    n = 0
    quote: str | None = None
    out: list[str] = []
    for ch in sql:
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in ("'", '"', "`", "["):
            quote = "]" if ch == "[" else ch
            out.append(ch)
        elif ch == "?":
            n += 1
            out.append(style(n))
        else:
            out.append(ch)
    return "".join(out)


def _percent_style(sql: str) -> str:
    """``?`` as ``%s``, a literal ``%`` doubled (the drivers format parameters with ``%``)."""
    return number_placeholders(sql.replace("%", "%%"), lambda _: "%s")


def _naive_utc(value: Any) -> Any:
    """Aware datetimes as naive UTC (MySQL ``datetime``, SQL Server ``datetime2``)."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def split_sqlite_script(script: str) -> list[str]:
    """A script as its complete statements (``sqlite3.complete_statement``), so each runs with ``execute``
    (``executescript`` would commit an open transaction first)."""
    statements: list[str] = []
    buf = ""
    for ch in script:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            if buf.strip().strip(";").strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip() and not all(line.strip().startswith("--") or not line.strip() for line in buf.splitlines()):
        statements.append(buf.strip())
    return statements


# ---------------------------------------------------------------------------
# SQLite (sqlite3)
# ---------------------------------------------------------------------------


class _SqliteSession:
    dialect = "sqlite"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        values = [
            v.isoformat() if isinstance(v, datetime) else int(v) if isinstance(v, bool) else v for v in params
        ]
        cursor = self._conn.execute(sql, values)
        if cursor.description is None:
            return []
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    async def exec(self, script: str) -> None:
        for statement in split_sqlite_script(script):
            self._conn.execute(statement)


class _SqliteConnection:
    dialect = "sqlite"

    def __init__(self, conn: sqlite3.Connection) -> None:
        # Explicit transactions (BEGIN IMMEDIATE ... COMMIT): no implicit ones around statements
        conn.isolation_level = None
        self._conn = conn
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_SqliteSession]:
        async with self._lock:
            yield _SqliteSession(self._conn)


def sqlite_connection(conn: sqlite3.Connection) -> CliSqlConnection:
    """A SQLite database from a ``sqlite3`` connection (one connection: sessions run one after another).
    The connection is switched to autocommit (``isolation_level = None``): migrations open their own
    transactions."""
    return _SqliteConnection(conn)


# ---------------------------------------------------------------------------
# PostgreSQL (asyncpg)
# ---------------------------------------------------------------------------


class _PostgresSession:
    dialect = "postgres"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        rows = await self._conn.fetch(number_placeholders(sql, lambda n: f"${n}"), *params)
        return [dict(r) for r in rows]

    async def exec(self, script: str) -> None:
        await self._conn.execute(script)


class _PostgresConnection:
    dialect = "postgres"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_PostgresSession]:
        async with self._pool.acquire() as conn:
            yield _PostgresSession(conn)


def postgres_connection(pool: Any) -> CliSqlConnection:
    """A PostgreSQL database from an ``asyncpg`` pool (``await asyncpg.create_pool(dsn)``)."""
    return _PostgresConnection(pool)


# ---------------------------------------------------------------------------
# MySQL / MariaDB (aiomysql)
# ---------------------------------------------------------------------------


class _MysqlSession:
    dialect = "mysql"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self._conn.cursor() as cursor:
            await cursor.execute(_percent_style(sql), [_naive_utc(p) for p in params] if params else None)
            if cursor.description is None:
                return []
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row)) for row in await cursor.fetchall()]

    async def exec(self, script: str) -> None:
        async with self._conn.cursor() as cursor:
            await cursor.execute(script)
            while await cursor.nextset():
                pass


class _MysqlConnection:
    dialect = "mysql"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_MysqlSession]:
        async with self._pool.acquire() as conn:
            await conn.autocommit(True)
            yield _MysqlSession(conn)


def mysql_connection(pool: Any) -> CliSqlConnection:
    """A MySQL / MariaDB database from an ``aiomysql`` pool.  Migration scripts with several statements need
    the pool created with ``client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS``.  MySQL commits DDL
    statements on its own: a failing migration that created a table leaves it behind."""
    return _MysqlConnection(pool)


# ---------------------------------------------------------------------------
# SQL Server (pymssql, run on a thread of its own per session)
# ---------------------------------------------------------------------------


def _split_batches(script: str) -> list[str]:
    import re

    return [b for b in re.split(r"^\s*GO\s*;?\s*$", script, flags=re.IGNORECASE | re.MULTILINE) if b.strip()]


class _MssqlSession:
    dialect = "mssql"

    def __init__(self, conn: Any, executor: ThreadPoolExecutor) -> None:
        self._conn = conn
        self._executor = executor

    async def _run(self, fn: Callable[[], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        def run() -> list[Row]:
            cursor = self._conn.cursor()
            try:
                if params:
                    cursor.execute(_percent_style(sql), tuple(_naive_utc(p) for p in params))
                else:
                    cursor.execute(sql)
                # The first result set with rows (a batch may run statements without one first)
                while cursor.description is None:
                    if not cursor.nextset():
                        return []
                names = [d[0] for d in cursor.description]
                rows = [dict(zip(names, row)) for row in cursor.fetchall()]
                while cursor.nextset():
                    pass
                return rows
            finally:
                cursor.close()

        return await self._run(run)

    async def exec(self, script: str) -> None:
        def run() -> None:
            cursor = self._conn.cursor()
            try:
                for batch in _split_batches(script):
                    cursor.execute(batch)
                    while cursor.nextset():
                        pass
            finally:
                cursor.close()

        await self._run(run)


class _MssqlConnection:
    dialect = "mssql"

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_MssqlSession]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qcli-mssql")
        loop = asyncio.get_running_loop()
        try:
            conn = await loop.run_in_executor(executor, self._connect)
            try:
                await loop.run_in_executor(executor, lambda: conn.autocommit(True))
                yield _MssqlSession(conn, executor)
            finally:
                await loop.run_in_executor(executor, conn.close)
        finally:
            executor.shutdown(wait=False)


def mssql_connection(connect: Callable[[], Any]) -> CliSqlConnection:
    """A SQL Server database from a function opening a ``pymssql`` connection
    (``lambda: pymssql.connect(server, user, password, database)``).  Each session opens its own connection,
    so locks and transactions stay on it; ``GO`` lines split a script into batches."""
    return _MssqlConnection(connect)
