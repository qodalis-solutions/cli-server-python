"""Databases the migrations tests run on: SQLite always; PostgreSQL, MySQL and SQL Server when
``QCLI_TEST_POSTGRES`` (``postgres://`` URL), ``QCLI_TEST_MYSQL`` (``mysql://`` URL) and ``QCLI_TEST_MSSQL``
(``mssql://user:password@host:port/database``) are set."""

from __future__ import annotations

import itertools
import os
import sqlite3
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

from qodalis_cli_databases import (
    CliSqlConnection,
    mssql_connection,
    mysql_connection,
    postgres_connection,
    sqlite_connection,
)

Opened = tuple[CliSqlConnection, Callable[[], Awaitable[None]]]


async def _sqlite() -> Opened:
    conn = sqlite3.connect(":memory:")

    async def close() -> None:
        conn.close()

    return sqlite_connection(conn), close


async def _postgres() -> Opened:
    import asyncpg

    pool = await asyncpg.create_pool(os.environ["QCLI_TEST_POSTGRES"], min_size=1, max_size=4)
    return postgres_connection(pool), pool.close


def _url(name: str) -> dict[str, Any]:
    url = urlsplit(os.environ[name])
    return {
        "host": url.hostname,
        "port": url.port,
        "user": unquote(url.username or ""),
        "password": unquote(url.password or ""),
        "database": url.path.lstrip("/"),
    }


async def _mysql() -> Opened:
    import aiomysql
    from pymysql.constants import CLIENT

    u = _url("QCLI_TEST_MYSQL")
    pool = await aiomysql.create_pool(
        host=u["host"], port=u["port"] or 3306, user=u["user"], password=u["password"], db=u["database"],
        client_flag=CLIENT.MULTI_STATEMENTS, minsize=1, maxsize=4,
    )

    async def close() -> None:
        pool.close()
        await pool.wait_closed()

    return mysql_connection(pool), close


async def _mssql() -> Opened:
    import pymssql

    u = _url("QCLI_TEST_MSSQL")

    async def close() -> None:
        return None

    return mssql_connection(lambda: pymssql.connect(
        server=u["host"], port=str(u["port"] or 1433), user=u["user"], password=u["password"], database=u["database"],
    )), close


TARGETS: list[tuple[str, Callable[[], Awaitable[Opened]]]] = [("sqlite", _sqlite)]
if os.environ.get("QCLI_TEST_POSTGRES"):
    TARGETS.append(("postgres", _postgres))
if os.environ.get("QCLI_TEST_MYSQL"):
    TARGETS.append(("mysql", _mysql))
if os.environ.get("QCLI_TEST_MSSQL"):
    TARGETS.append(("mssql", _mssql))

_counter = itertools.count()


def unique() -> str:
    """A table prefix of its own per test (dropped afterwards)."""
    return f"T{os.getpid() % 1000}{next(_counter)}{os.urandom(2).hex()}"


def q(dialect: str, name: str) -> str:
    return f"`{name}`" if dialect == "mysql" else f"[{name}]" if dialect == "mssql" else f'"{name}"'


def create_table(t: str, extra: str = "") -> dict[str, str]:
    """SQL per dialect for a table ``t`` with an ``Id`` column (``extra``: more columns, double-quoted)."""
    import re

    mssql_extra = re.sub(r'"([^"]+)"', r"[\1]", extra)
    mysql_extra = extra.replace('"', "`")
    return {
        "postgres": f'CREATE TABLE "{t}" ("Id" integer PRIMARY KEY{extra})',
        "sqlite": f'CREATE TABLE "{t}" ("Id" INTEGER PRIMARY KEY{extra})',
        "mysql": f"CREATE TABLE `{t}` (`Id` int PRIMARY KEY{mysql_extra})",
        "mssql": f"CREATE TABLE [{t}] ([Id] int PRIMARY KEY{mssql_extra})",
    }


def drop_table(t: str) -> dict[str, str]:
    return {d: f"DROP TABLE {q(d, t)}" for d in ("postgres", "sqlite", "mysql", "mssql")}


async def drop_prefixed(connection: CliSqlConnection, prefix: str) -> None:
    """Drops every table whose name starts with ``prefix``."""
    sql = {
        "postgres": "SELECT table_name AS t FROM information_schema.tables WHERE table_schema = current_schema() AND table_name LIKE ?",
        "mysql": "SELECT table_name AS t FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name LIKE ?",
        "mssql": "SELECT TABLE_NAME AS t FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE ?",
        "sqlite": "SELECT name AS t FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
    }[connection.dialect]
    async with connection.session() as db:
        for row in await db.query(sql, [f"{prefix}%"]):
            name = row.get("t") or row.get("T") or row.get("TABLE_NAME")
            await db.exec(drop_table(name)[db.dialect])
