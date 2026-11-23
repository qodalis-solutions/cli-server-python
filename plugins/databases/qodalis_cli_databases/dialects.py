"""SQL per dialect: quoting, the history tables, schema checks, renames and the migration lock."""

from __future__ import annotations

from dataclasses import dataclass

from qodalis_cli_server_abstractions import is_sql_identifier

from .connection import CliSqlSession


def _checked(name: str) -> str:
    if not is_sql_identifier(name):
        raise ValueError(f"invalid identifier: {name}")
    return name


def quote_ident(dialect: str, name: str) -> str:
    """An identifier quoted for the dialect (validated first, so a name can never inject SQL)."""
    n = _checked(name)
    if dialect == "mysql":
        return f"`{n}`"
    if dialect == "mssql":
        return f"[{n}]"
    return f'"{n}"'


def quote_table(dialect: str, table: str, schema: str | None = None) -> str:
    """A table name with its schema, quoted (``"qcli"."QcliFiles"``); SQLite and MySQL have no schemas here."""
    t = quote_ident(dialect, table)
    if schema and dialect in ("postgres", "mssql"):
        return f"{quote_ident(dialect, schema)}.{t}"
    return t


#: Column types the history tables use, by dialect.
_TYPES: dict[str, dict[str, str]] = {
    "postgres": {"key": "varchar(150)", "text": "varchar(255)", "long": "text", "time": "timestamptz", "int": "integer"},
    "mysql": {"key": "varchar(150)", "text": "varchar(255)", "long": "longtext", "time": "datetime(3)", "int": "int"},
    "mssql": {"key": "nvarchar(150)", "text": "nvarchar(255)", "long": "nvarchar(max)", "time": "datetime2", "int": "int"},
    "sqlite": {"key": "TEXT", "text": "TEXT", "long": "TEXT", "time": "TEXT", "int": "INTEGER"},
}


@dataclass(frozen=True)
class HistoryTables:
    migrations: str
    runs: str


async def ensure_history_tables(db: CliSqlSession, tables: HistoryTables, schema: str | None = None) -> None:
    """Creates the history tables when missing (``QcliMigrations``, ``QcliMigrationRuns``)."""
    d = db.dialect
    t = _TYPES[d]

    def q(name: str) -> str:
        return quote_ident(d, name)

    async def create(table: str, columns: list[str]) -> None:
        if await table_exists(db, table, schema):
            return
        await db.exec(f"CREATE TABLE {quote_table(d, table, schema)} ({', '.join(columns)})")

    await create(tables.migrations, [
        f"{q('Source')} {t['key']} NOT NULL",
        f"{q('Id')} {t['key']} NOT NULL",
        f"{q('Name')} {t['text']} NOT NULL",
        f"{q('Checksum')} {t['key']} NULL",
        f"{q('AppliedAt')} {t['time']} NOT NULL",
        f"{q('DurationMs')} {t['int']} NOT NULL",
        f"{q('AppliedBy')} {t['text']} NULL",
        f"{q('RunId')} {t['key']} NULL",
        f"CONSTRAINT {q('PK_' + tables.migrations)} PRIMARY KEY ({q('Source')}, {q('Id')})",
    ])
    await create(tables.runs, [
        f"{q('Id')} {t['key']} NOT NULL",
        f"{q('Direction')} {t['key']} NOT NULL",
        f"{q('Target')} {t['key']} NULL",
        f"{q('Trigger')} {t['key']} NOT NULL",
        f"{q('Actor')} {t['text']} NULL",
        f"{q('StartedAt')} {t['time']} NOT NULL",
        f"{q('FinishedAt')} {t['time']} NULL",
        f"{q('Status')} {t['key']} NOT NULL",
        f"{q('Error')} {t['long']} NULL",
        f"{q('Applied')} {t['long']} NULL",
        f"CONSTRAINT {q('PK_' + tables.runs)} PRIMARY KEY ({q('Id')})",
    ])


async def table_exists(db: CliSqlSession, table: str, schema: str | None = None) -> bool:
    """True when the table exists (in ``schema``, or the connection's default schema)."""
    _checked(table)
    d = db.dialect
    if d == "postgres":
        rows = await db.query(
            "SELECT 1 AS x FROM information_schema.tables WHERE table_name = ? "
            "AND table_schema = COALESCE(?::text, current_schema())",
            [table, schema],
        )
    elif d == "mysql":
        rows = await db.query(
            "SELECT 1 AS x FROM information_schema.tables WHERE table_name = ? AND table_schema = DATABASE()", [table],
        )
    elif d == "mssql":
        rows = await db.query(
            "SELECT 1 AS x FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ? AND TABLE_SCHEMA = COALESCE(?, SCHEMA_NAME())",
            [table, schema],
        )
    else:
        rows = await db.query("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", [table])
    return len(rows) > 0


async def column_exists(db: CliSqlSession, table: str, column: str, schema: str | None = None) -> bool:
    """True when the table has the column."""
    _checked(table)
    _checked(column)
    d = db.dialect
    if d == "postgres":
        rows = await db.query(
            "SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ? "
            "AND table_schema = COALESCE(?::text, current_schema())",
            [table, column, schema],
        )
    elif d == "mysql":
        rows = await db.query(
            "SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ? "
            "AND table_schema = DATABASE()",
            [table, column],
        )
    elif d == "mssql":
        rows = await db.query(
            "SELECT 1 AS x FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? AND COLUMN_NAME = ? "
            "AND TABLE_SCHEMA = COALESCE(?, SCHEMA_NAME())",
            [table, column, schema],
        )
    else:
        return any(c["name"] == column for c in await db.query(f"PRAGMA table_info({quote_ident('sqlite', table)})"))
    return len(rows) > 0


async def rename_table(db: CliSqlSession, source: str, to: str, schema: str | None = None) -> None:
    """Renames a table (in its schema)."""
    d = db.dialect
    if d == "mssql":
        name = f"{_checked(schema)}.{_checked(source)}" if schema else _checked(source)
        await db.query("EXEC sp_rename ?, ?", [name, _checked(to)])
    elif d == "mysql":
        await db.exec(f"RENAME TABLE {quote_ident(d, source)} TO {quote_ident(d, to)}")
    else:
        await db.exec(f"ALTER TABLE {quote_table(d, source, schema)} RENAME TO {quote_ident(d, to)}")


async def rename_column(db: CliSqlSession, table: str, source: str, to: str, schema: str | None = None) -> None:
    """Renames a column (SQLite 3.25 and later)."""
    d = db.dialect
    if d == "mssql":
        name = f"{_checked(schema) + '.' if schema else ''}{_checked(table)}.{_checked(source)}"
        await db.query("EXEC sp_rename ?, ?, 'COLUMN'", [name, _checked(to)])
    else:
        await db.exec(
            f"ALTER TABLE {quote_table(d, table, schema)} RENAME COLUMN {quote_ident(d, source)} TO {quote_ident(d, to)}"
        )


async def adopt_legacy_table(
    db: CliSqlSession,
    legacy: str,
    table: str,
    columns: dict[str, str] | None = None,
    schema: str | None = None,
) -> bool:
    """Moves a table of a previous release to its new name and columns (cli-server-dotnet#99): when ``legacy``
    exists and ``table`` does not, renames the table, then each column of ``columns`` (old -> new) present
    under its old name.  Returns True when it renamed anything."""
    if legacy == table or not await table_exists(db, legacy, schema) or await table_exists(db, table, schema):
        return False
    await rename_table(db, legacy, table, schema)
    for source, to in (columns or {}).items():
        if source != to and await column_exists(db, table, source, schema):
            await rename_column(db, table, source, to, schema)
    return True


#: Statements that open, commit and roll back a transaction.
TRANSACTION: dict[str, dict[str, str]] = {
    "postgres": {"begin": "BEGIN", "commit": "COMMIT", "rollback": "ROLLBACK"},
    "mysql": {"begin": "START TRANSACTION", "commit": "COMMIT", "rollback": "ROLLBACK"},
    "mssql": {
        "begin": "BEGIN TRANSACTION",
        "commit": "COMMIT TRANSACTION",
        "rollback": "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION",
    },
    "sqlite": {"begin": "BEGIN IMMEDIATE", "commit": "COMMIT", "rollback": "ROLLBACK"},
}


async def acquire_lock(db: CliSqlSession, key: str, wait: bool) -> bool:
    """Takes the database's migration lock on the session: waits for it (``wait``), or returns False at once
    when another process holds it.  SQLite has one writer per database file and no session locks: always
    True."""
    d = db.dialect
    if d == "postgres":
        fn = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
        rows = await db.query(f"SELECT {fn}(hashtext(?::text))::text AS ok", [key])
        return wait or (rows[0]["ok"] if rows else None) in ("true", "t")
    if d == "mysql":
        rows = await db.query("SELECT GET_LOCK(?, ?) AS ok", [key, -1 if wait else 0])
        return rows[0]["ok"] is not None and int(rows[0]["ok"]) == 1
    if d == "mssql":
        rows = await db.query(
            "DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = ?; SELECT @r AS ok",
            [key, -1 if wait else 0],
        )
        return bool(rows) and int(rows[0]["ok"]) >= 0
    return True


async def release_lock(db: CliSqlSession, key: str) -> None:
    """Releases the lock :func:`acquire_lock` took."""
    d = db.dialect
    if d == "postgres":
        await db.query("SELECT pg_advisory_unlock(hashtext(?::text))::text AS ok", [key])
    elif d == "mysql":
        await db.query("SELECT RELEASE_LOCK(?) AS ok", [key])
    elif d == "mssql":
        await db.query("EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session'", [key])
