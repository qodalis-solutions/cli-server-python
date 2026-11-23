"""Database stats: server, size, connections, load counters, cache hit ratio and the largest tables, per dialect.

Each metric group is read on its own: one that fails (a missing permission, e.g. SQL Server without
``VIEW SERVER STATE``) is left out and listed in ``unavailable``, so the rest still shows.  Counters are
cumulative since the server started; the dashboard turns two samples into rates.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from .connection import CliSqlConnection, CliSqlSession

logger = logging.getLogger(__name__)

#: How many tables the stats list (the largest).
TOP_TABLES = 10

#: SQLite counts rows of at most this many tables (``count(*)`` reads the whole table).
SQLITE_COUNTED_TABLES = 20


def _num(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value is not None else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    """Without the keys whose value is unknown."""
    return {k: v for k, v in values.items() if v is not None}


def _top(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The largest tables: by bytes when known, else by rows."""
    key = "bytes" if any(t.get("bytes") is not None for t in tables) else "rows"
    tables.sort(key=lambda t: t.get(key) or 0, reverse=True)
    return [_clean(t) for t in tables[:TOP_TABLES]]


def _uptime(started: Any) -> int | None:
    if not isinstance(started, datetime):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((_now() - started).total_seconds()))


Group = Callable[[CliSqlSession], Awaitable[Any]]


# ---------------------------------------------------------------- PostgreSQL

async def _pg_server(db: CliSqlSession) -> dict[str, Any]:
    row = _first(await db.query("SELECT version() AS version, pg_postmaster_start_time() AS started"))
    version = str(row.get("version") or "")
    # "PostgreSQL 17.2 on x86_64-pc-linux-gnu, ..." -> "PostgreSQL 17.2"
    short = " ".join(version.split(" ")[:2]) if version.startswith("PostgreSQL") else version
    started = row.get("started")
    return _clean({"version": short or None, "startedAt": _iso(started), "uptimeSeconds": _uptime(started)})


async def _pg_size(db: CliSqlSession) -> dict[str, Any]:
    row = _first(await db.query(
        "SELECT pg_database_size(current_database()) AS bytes, "
        "(SELECT COALESCE(SUM(pg_indexes_size(relid)), 0) FROM pg_stat_user_tables) AS index_bytes"
    ))
    total, index = _num(row.get("bytes")), _num(row.get("index_bytes"))
    return _clean({"bytes": total, "indexBytes": index, "dataBytes": total - index if total is not None and index is not None else None})


async def _pg_database_row(db: CliSqlSession) -> dict[str, Any]:
    return _first(await db.query(
        "SELECT numbackends, xact_commit, xact_rollback, blks_hit, blks_read, tup_returned, tup_fetched, "
        "tup_inserted, tup_updated, tup_deleted FROM pg_stat_database WHERE datname = current_database()"
    ))


async def _pg_connections(db: CliSqlSession) -> dict[str, Any]:
    row = _first(await db.query(
        "SELECT (SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database()) AS open, "
        "(SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active') AS active, "
        "current_setting('max_connections') AS max"
    ))
    return _clean({"active": _num(row.get("active")), "open": _num(row.get("open")), "max": _num(row.get("max"))})


async def _pg_counters(db: CliSqlSession) -> dict[str, Any]:
    row = await _pg_database_row(db)
    commits, rollbacks = _num(row.get("xact_commit")), _num(row.get("xact_rollback"))
    read = [_num(row.get(k)) for k in ("tup_returned", "tup_fetched")]
    written = [_num(row.get(k)) for k in ("tup_inserted", "tup_updated", "tup_deleted")]
    return _clean({
        "transactions": (commits or 0) + (rollbacks or 0) if commits is not None else None,
        "commits": commits,
        "rollbacks": rollbacks,
        "rowsRead": sum(v or 0 for v in read) if any(v is not None for v in read) else None,
        "rowsWritten": sum(v or 0 for v in written) if any(v is not None for v in written) else None,
    })


async def _pg_cache(db: CliSqlSession) -> float | None:
    row = await _pg_database_row(db)
    hit, read = _num(row.get("blks_hit")) or 0, _num(row.get("blks_read")) or 0
    return hit / (hit + read) if hit + read else None


async def _pg_tables(db: CliSqlSession) -> list[dict[str, Any]]:
    rows = await db.query(
        "SELECT schemaname AS schema_name, relname AS table_name, n_live_tup AS row_count, "
        "pg_total_relation_size(relid) AS bytes FROM pg_stat_user_tables "
        f"ORDER BY pg_total_relation_size(relid) DESC LIMIT {TOP_TABLES}"
    )
    return _top([
        {"schema": r.get("schema_name"), "name": r.get("table_name"), "rows": _num(r.get("row_count")), "bytes": _num(r.get("bytes"))}
        for r in rows
    ])


# ---------------------------------------------------------------- MySQL / MariaDB

_MYSQL_STATUS = (
    "Uptime", "Threads_connected", "Threads_running", "Questions", "Com_commit", "Com_rollback", "Innodb_rows_read",
    "Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_rows_deleted", "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
)


async def _mysql_status(db: CliSqlSession) -> dict[str, Any]:
    names = ", ".join(f"'{n}'" for n in _MYSQL_STATUS)
    rows = await db.query(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({names})")
    out: dict[str, Any] = {}
    for r in rows:
        values = list(r.values())
        name = r.get("Variable_name", values[0] if values else None)
        value = r.get("Value", values[1] if len(values) > 1 else None)
        if name is not None:
            out[str(name)] = _num(value)
    return out


async def _mysql_server(db: CliSqlSession) -> dict[str, Any]:
    version = _first(await db.query("SELECT VERSION() AS version")).get("version")
    uptime = (await _mysql_status(db)).get("Uptime")
    started = None
    if uptime is not None:
        started = datetime.fromtimestamp(_now().timestamp() - uptime, timezone.utc).replace(microsecond=0)
    prefix = "MariaDB " if version and "mariadb" in str(version).lower() else "MySQL "
    return _clean({
        "version": f"{prefix}{str(version).split('-')[0]}" if version else None,
        "startedAt": _iso(started),
        "uptimeSeconds": int(uptime) if uptime is not None else None,
    })


async def _mysql_fresh_table_stats(db: CliSqlSession) -> None:
    """information_schema.tables caches InnoDB sizes for a day (MySQL 8); read them fresh. MariaDB has no such
    cache (nor the variable)."""
    try:
        await db.query("SET SESSION information_schema_stats_expiry = 0")
    except Exception:  # noqa: BLE001 - MariaDB, older MySQL
        pass


async def _mysql_size(db: CliSqlSession) -> dict[str, Any]:
    await _mysql_fresh_table_stats(db)
    row = _first(await db.query(
        "SELECT COALESCE(SUM(data_length), 0) AS data_bytes, COALESCE(SUM(index_length), 0) AS index_bytes "
        "FROM information_schema.tables WHERE table_schema = DATABASE()"
    ))
    data, index = _num(row.get("data_bytes")), _num(row.get("index_bytes"))
    return _clean({"bytes": (data or 0) + (index or 0), "dataBytes": data, "indexBytes": index})


async def _mysql_connections(db: CliSqlSession) -> dict[str, Any]:
    # This database's connections (all of them with the PROCESS privilege, else the user's own)
    row = _first(await db.query(
        "SELECT COUNT(*) AS open_count, COALESCE(SUM(command NOT IN ('Sleep', 'Daemon')), 0) AS active_count "
        "FROM information_schema.processlist WHERE db = DATABASE()"
    ))
    max_connections = _num(_first(await db.query("SELECT @@max_connections AS max")).get("max"))
    return _clean({"active": _num(row.get("active_count")), "open": _num(row.get("open_count")), "max": max_connections})


async def _mysql_counters(db: CliSqlSession) -> dict[str, Any]:
    s = await _mysql_status(db)
    commits, rollbacks = s.get("Com_commit"), s.get("Com_rollback")
    written = [s.get(k) for k in ("Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_rows_deleted")]
    return _clean({
        "queries": s.get("Questions"),
        "transactions": (commits or 0) + (rollbacks or 0) if commits is not None else None,
        "commits": commits,
        "rollbacks": rollbacks,
        "rowsRead": s.get("Innodb_rows_read"),
        "rowsWritten": sum(v or 0 for v in written) if any(v is not None for v in written) else None,
    })


async def _mysql_cache(db: CliSqlSession) -> float | None:
    s = await _mysql_status(db)
    requests, reads = s.get("Innodb_buffer_pool_read_requests"), s.get("Innodb_buffer_pool_reads")
    return max(0.0, 1 - (reads or 0) / requests) if requests else None


async def _mysql_tables(db: CliSqlSession) -> list[dict[str, Any]]:
    await _mysql_fresh_table_stats(db)
    rows = await db.query(
        "SELECT table_name AS table_name, table_rows AS row_count, data_length + index_length AS bytes "
        "FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
        f"ORDER BY data_length + index_length DESC LIMIT {TOP_TABLES}"
    )
    return _top([
        {"name": r.get("table_name") or r.get("TABLE_NAME"), "rows": _num(r.get("row_count")), "bytes": _num(r.get("bytes"))}
        for r in rows
    ])


# ---------------------------------------------------------------- SQL Server

async def _mssql_server(db: CliSqlSession) -> dict[str, Any]:
    row = _first(await db.query(
        "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS version, "
        "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition"
    ))
    out: dict[str, Any] = {}
    if row.get("version"):
        out["version"] = f"SQL Server {row['version']}" + (f" ({row['edition']})" if row.get("edition") else "")
    # Needs VIEW SERVER STATE: without it the version alone is shown
    try:
        started = _first(await db.query("SELECT sqlserver_start_time AS started FROM sys.dm_os_sys_info")).get("started")
        out.update(_clean({"startedAt": _iso(started), "uptimeSeconds": _uptime(started)}))
    except Exception:  # noqa: BLE001 - optional
        pass
    return out


async def _mssql_size(db: CliSqlSession) -> dict[str, Any]:
    row = _first(await db.query(
        "SELECT CAST(SUM(CASE WHEN type = 0 THEN CAST(size AS bigint) ELSE 0 END) * 8192 AS bigint) AS data_bytes, "
        "CAST(SUM(CASE WHEN type = 1 THEN CAST(size AS bigint) ELSE 0 END) * 8192 AS bigint) AS log_bytes, "
        "CAST(SUM(CAST(size AS bigint)) * 8192 AS bigint) AS total_bytes FROM sys.database_files"
    ))
    out = _clean({"bytes": _num(row.get("total_bytes")), "dataBytes": _num(row.get("data_bytes")), "logBytes": _num(row.get("log_bytes"))})
    try:
        index = _first(await db.query(
            "SELECT CAST(COALESCE(SUM(used_page_count), 0) * 8192 AS bigint) AS index_bytes "
            "FROM sys.dm_db_partition_stats WHERE index_id > 1"
        )).get("index_bytes")
        if index is not None:
            out["indexBytes"] = _num(index)
    except Exception:  # noqa: BLE001 - optional
        pass
    return out


async def _mssql_connections(db: CliSqlSession) -> dict[str, Any]:
    row = _first(await db.query(
        "SELECT (SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE database_id = DB_ID() AND is_user_process = 1) AS open_count, "
        "(SELECT COUNT(*) FROM sys.dm_exec_requests WHERE database_id = DB_ID() AND session_id <> @@SPID) AS active_count, "
        "@@MAX_CONNECTIONS AS max_count"
    ))
    return _clean({"active": _num(row.get("active_count")), "open": _num(row.get("open_count")), "max": _num(row.get("max_count"))})


async def _mssql_counters(db: CliSqlSession) -> dict[str, Any]:
    rows = await db.query(
        "SELECT RTRIM(counter_name) AS counter_name, RTRIM(instance_name) AS instance_name, cntr_value AS value "
        "FROM sys.dm_os_performance_counters WHERE (counter_name = 'Batch Requests/sec' AND object_name LIKE '%SQL Statistics%') "
        "OR (counter_name = 'Transactions/sec' AND object_name LIKE '%:Databases%' AND instance_name = DB_NAME())"
    )
    if not rows:
        # The view is empty without VIEW SERVER STATE
        raise PermissionError("performance counters are not visible (VIEW SERVER STATE)")
    out: dict[str, Any] = {}
    for r in rows:
        if r.get("counter_name") == "Batch Requests/sec":
            out["queries"] = _num(r.get("value"))
        elif r.get("counter_name") == "Transactions/sec":
            out["transactions"] = _num(r.get("value"))
    return out


async def _mssql_cache(db: CliSqlSession) -> float | None:
    rows = await db.query(
        "SELECT RTRIM(counter_name) AS counter_name, cntr_value AS value FROM sys.dm_os_performance_counters "
        "WHERE object_name LIKE '%Buffer Manager%' AND counter_name IN ('Buffer cache hit ratio', 'Buffer cache hit ratio base')"
    )
    if not rows:
        raise PermissionError("performance counters are not visible (VIEW SERVER STATE)")
    values = {r.get("counter_name"): _num(r.get("value")) for r in rows}
    ratio, base = values.get("Buffer cache hit ratio"), values.get("Buffer cache hit ratio base")
    return min(1.0, ratio / base) if ratio is not None and base else None


async def _mssql_tables(db: CliSqlSession) -> list[dict[str, Any]]:
    rows = await db.query(
        f"SELECT TOP {TOP_TABLES} s.name AS schema_name, t.name AS table_name, "
        "SUM(CASE WHEN p.index_id IN (0, 1) THEN p.row_count ELSE 0 END) AS row_count, "
        "CAST(SUM(p.reserved_page_count) * 8192 AS bigint) AS bytes "
        "FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "JOIN sys.dm_db_partition_stats p ON p.object_id = t.object_id "
        "GROUP BY s.name, t.name ORDER BY SUM(p.reserved_page_count) DESC"
    )
    return _top([
        {"schema": r.get("schema_name"), "name": r.get("table_name"), "rows": _num(r.get("row_count")), "bytes": _num(r.get("bytes"))}
        for r in rows
    ])


# ---------------------------------------------------------------- SQLite

async def _sqlite_server(db: CliSqlSession) -> dict[str, Any]:
    version = _first(await db.query("SELECT sqlite_version() AS version")).get("version")
    return {"version": f"SQLite {version}"} if version else {}


async def _sqlite_size(db: CliSqlSession) -> dict[str, Any]:
    pages = _num(_first(await db.query("PRAGMA page_count")).get("page_count"))
    page_size = _num(_first(await db.query("PRAGMA page_size")).get("page_size"))
    return {"bytes": pages * page_size} if pages is not None and page_size is not None else {}


async def _sqlite_tables(db: CliSqlSession) -> list[dict[str, Any]]:
    names = [
        r["name"] for r in await db.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    sizes: dict[str, int] = {}
    try:
        # dbstat is compiled in most builds; indexes count to their table
        for r in await db.query(
            "SELECT COALESCE(m.tbl_name, s.name) AS table_name, SUM(s.pgsize) AS bytes FROM dbstat s "
            "LEFT JOIN sqlite_master m ON m.name = s.name GROUP BY COALESCE(m.tbl_name, s.name)"
        ):
            sizes[str(r["table_name"])] = int(r["bytes"] or 0)
    except Exception:  # noqa: BLE001 - no dbstat: rows only
        pass
    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'  # any table name, not only identifiers

    if sizes:
        ordered = sorted(names, key=lambda n: sizes.get(n, 0), reverse=True)
    else:
        # Without sizes, every table is ranked by its highest rowid (an index lookup, not a scan) before the counts
        estimate: dict[str, int] = {}
        for name in names:
            try:
                estimate[name] = _num(_first(await db.query(f"SELECT MAX(rowid) AS n FROM {quote(name)}")).get("n")) or 0
            except Exception:  # noqa: BLE001 - a WITHOUT ROWID table
                estimate[name] = 0
        ordered = sorted(names, key=lambda n: estimate[n], reverse=True)
    tables: list[dict[str, Any]] = []
    for i, name in enumerate(ordered):
        rows = None
        if i < SQLITE_COUNTED_TABLES:
            rows = _num(_first(await db.query(f"SELECT COUNT(*) AS n FROM {quote(name)}")).get("n"))
        tables.append({"name": name, "rows": rows, "bytes": sizes.get(name) if sizes else None})
    return _top(tables)


# ---------------------------------------------------------------- collection

_GROUPS: dict[str, dict[str, Group]] = {
    "postgres": {
        "server": _pg_server, "size": _pg_size, "connections": _pg_connections, "counters": _pg_counters,
        "cacheHitRatio": _pg_cache, "tables": _pg_tables,
    },
    "mysql": {
        "server": _mysql_server, "size": _mysql_size, "connections": _mysql_connections, "counters": _mysql_counters,
        "cacheHitRatio": _mysql_cache, "tables": _mysql_tables,
    },
    "mssql": {
        "server": _mssql_server, "size": _mssql_size, "connections": _mssql_connections, "counters": _mssql_counters,
        "cacheHitRatio": _mssql_cache, "tables": _mssql_tables,
    },
    "sqlite": {"server": _sqlite_server, "size": _sqlite_size, "tables": _sqlite_tables},
}

_COUNTERS_SCOPE = {"postgres": "database", "mysql": "server", "mssql": "server"}


async def collect_stats(name: str, connection: CliSqlConnection) -> dict[str, Any]:
    """The stats of a database (``GET /api/v1/qcli/databases/{db}/stats``); never raises for a metric."""
    dialect = connection.dialect
    stats: dict[str, Any] = {"name": name, "dialect": dialect, "collectedAt": _iso(_now())}
    unavailable: list[dict[str, str]] = []
    groups = _GROUPS.get(dialect, {})
    try:
        async with connection.session() as db:
            for metric, read in groups.items():
                try:
                    value = await read(db)
                except Exception as exc:  # noqa: BLE001 - listed as unavailable
                    unavailable.append({"metric": metric, "error": str(exc) or type(exc).__name__})
                    continue
                if value is None or value == {}:
                    continue
                stats[metric] = value
    except Exception as exc:  # noqa: BLE001 - the database is down
        unavailable.append({"metric": "connection", "error": str(exc) or type(exc).__name__})
    if "counters" in stats and dialect in _COUNTERS_SCOPE:
        stats["countersScope"] = _COUNTERS_SCOPE[dialect]
    stats["unavailable"] = unavailable
    return stats


async def collect_size(connection: CliSqlConnection) -> int | None:
    """The database's size in bytes, best effort (the summary's ``sizeBytes``)."""
    read = _GROUPS.get(connection.dialect, {}).get("size")
    if read is None:
        return None
    try:
        async with connection.session() as db:
            size = await read(db)
    except Exception as exc:  # noqa: BLE001 - omitted from the summary
        logger.debug("could not read the size of a %s database: %s", connection.dialect, exc)
        return None
    value = size.get("bytes") if isinstance(size, dict) else None
    return int(value) if value is not None else None
