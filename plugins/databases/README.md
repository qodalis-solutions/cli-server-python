# qodalis-cli-server-plugin-databases

The server's relational databases (PostgreSQL, MySQL/MariaDB, SQL Server and SQLite) for the Qodalis CLI server:
their stats (size, connections, load, largest tables) and their migrations. Migrations are versioned schema changes,
applied under a lock and recorded with who ran them, when and how long they took. You see the databases, and run and
roll back their migrations, from the admin dashboard, the web terminal (`databases`), MCP and the API. The Node and
.NET servers have the same plugin, with the same API, tables and commands.

```bash
pip install qodalis-cli-server-plugin-databases[postgres]   # or [mysql], [mssql]; SQLite needs nothing
```

## Use

```python
from contextlib import asynccontextmanager

import asyncpg
from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_databases import CliDatabasesBuilder, CliMigrationDatabaseOptions, postgres_connection


@asynccontextmanager
async def lifespan(app):
    pool = await asyncpg.create_pool(DATABASE_URL)
    databases = (
        CliDatabasesBuilder()
        .add_database("main", CliMigrationDatabaseOptions(postgres_connection(pool), apply_on_startup=True))
        .add_sql_folder("main", "./migrations")
        .build()
    )
    await databases.apply_on_startup()  # pending migrations before serving; a failure stops startup
    result.mount_plugin(databases)
    yield
```

Connections:

| Function | Driver | Notes |
|---|---|---|
| `postgres_connection(pool)` | `asyncpg` pool | |
| `mysql_connection(pool)` | `aiomysql` pool | Create the pool with `client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS`. |
| `mssql_connection(lambda: pymssql.connect(...))` | `pymssql` | One connection per session, on its own thread. |
| `sqlite_connection(conn)` | `sqlite3` | The connection is switched to autocommit; migrations open their own transactions. |

The plugin needs none of the drivers itself. A database with no migrations is still listed with its stats.

## Stats

`GET /api/v1/qcli/databases/{db}/stats` (terminal `databases stats <db>`, MCP `databases_stats`) returns:

| Field | Content |
|---|---|
| `server` | Version, start time, uptime. |
| `size` | Total bytes, with data, index and (SQL Server) log bytes. |
| `connections` | Active, open and max connections to the database. |
| `counters` | Queries, transactions, commits, rollbacks, rows read and written. Cumulative since the server started; `countersScope` says whether they count the database (`database`, PostgreSQL) or the whole server (`server`, MySQL and SQL Server). |
| `cacheHitRatio` | 0 to 1. |
| `tables` | The 10 largest tables, with rows and bytes. |

The dashboard turns two samples of `counters` into rates.

Each group is read on its own. A group the database user may not read is left out and listed in `unavailable` with
the error, and the rest still shows. On SQL Server, for example, connections and counters need `VIEW SERVER STATE`.
SQLite has no server, connections or counters. Its table sizes come from `dbstat` when SQLite was built with it, and
it counts rows for the 20 largest tables.

## Migrations

- **A folder of SQL files**: `0001_create_users.up.sql`, with an optional `0001_create_users.down.sql` to roll it
  back. A file starting with `-- qcli:no-transaction` runs outside a transaction (e.g. `CREATE INDEX CONCURRENTLY`).
- **Code**: `add_migrations("main", "billing", [CliMigration("1", "create invoices", "CREATE TABLE ...", "DROP TABLE ...")])`.
  `up`/`down` can be SQL, SQL per dialect (`{"postgres": "...", "sqlite": "..."}`) or a coroutine function given
  the session (`await db.query(sql, params)`, `await db.exec(script)`). This is how plugins ship their schema.

A database can have several sources; they apply in the order they were added. Never edit or reorder a released
migration; add a new one instead. Each migration runs in its own transaction together with its history row. MySQL
commits DDL statements on its own, so a failing MySQL migration may leave a table behind.

## Status, runs and safety

- `QcliMigrations` holds the applied migrations: source, id, name, checksum, applied at/by and duration.
  `QcliMigrationRuns` holds every run: direction, target, trigger, who, start/end, status, error and what it applied.
  The names follow the plugin table names (`table_prefix`, `schema` and per-table `tables` overrides on
  `CliMigrationDatabaseOptions`).
- A migration is `applied`, `pending`, `changed` (its SQL differs from what was applied), `missing` (applied, no
  longer in its source) or `failed` (the last run stopped at it). Runs are refused while one is `changed` or
  `missing`, unless `force`.
- One run at a time: a lock per database (`pg_advisory_lock`, `GET_LOCK`, `sp_getapplock`) across processes, so two
  servers starting together apply once. The API, terminal and MCP answer busy (409); `apply_on_startup` waits.
- Every run publishes `databases:migration-run` (audience `databases:read`). With `set_audit_store(store)`, it also
  writes `migrations.up` / `migrations.down` to the audit log.

## API, terminal and MCP

| | |
|---|---|
| `GET /api/v1/qcli/databases` | databases: dialect, size, applied, pending, problems, last run |
| `GET /api/v1/qcli/databases/{db}` | one database, as in the list |
| `GET /api/v1/qcli/databases/{db}/stats` | server, size, connections, counters, cache hit ratio, largest tables |
| `GET /api/v1/qcli/databases/{db}/migrations` | its migrations and their status |
| `GET /api/v1/qcli/databases/{db}/migrations/runs?page=&size=` | run history, newest first |
| `GET /api/v1/qcli/databases/{db}/migrations/sql?direction=up\|down&to=` | the SQL a run would execute (dry run) |
| `POST /api/v1/qcli/databases/{db}/migrations/up` `{ to?, force? }` | apply pending migrations |
| `POST /api/v1/qcli/databases/{db}/migrations/down` `{ to }` | roll back to `to` (kept; `base` for everything) |

Permissions on a protected server: `databases:read` to read, `databases:migrate` to run migrations.

Terminal: `databases` (alias `db`):

- `databases list`
- `databases stats <db>`
- `databases migrations <db>`
- `databases up <db> [--to <id>] [--dry-run] [--force]`
- `databases down <db> --to <id> [--dry-run]`

With the server's MCP endpoint on, AI clients get these tools; runs they start are recorded with trigger `mcp`:

- `databases:read`: `databases_list`, `databases_stats`, `migrations_status`, `migrations_runs`, `migrations_sql`
- `databases:migrate`: `migrations_up`, `migrations_down`

## Dashboard page

The admin dashboard shows a Databases page (group Data, `databases:read`): the databases with their size. For each
one, it shows an overview (size, connections, load over time, largest tables), its migrations by status and the runs.
With `databases:migrate`, it adds Apply pending, Migrate to here and Roll back to here, each showing the SQL the run
executes.

The page is `@qodalis/cli-dashboard-module-databases`. It is copied into the package at build
(`scripts/copy_dashboard.py`), served at `/qcli/databases` and registered as a dashboard extension when mounted, with cards on the dashboard's
Overview (`widgets`: `overview`).
`set_dashboard_dir(path)` serves another build, and `set_dashboard_dir(False)` leaves the page out.

## Config store

`CliDatabaseConfigStore(connection, table_prefix=None, schema=None, tables=None)` keeps the server's writable
settings (config providers) in the table `QcliConfig`: one row per provider section (`Provider`, `Section`,
`Version`, `Data` as JSON, `UpdatedAt`), a change in a transaction with the row locked (`FOR UPDATE` on PostgreSQL and
MySQL, `UPDLOCK, HOLDLOCK` on SQL Server, `BEGIN IMMEDIATE` on SQLite), so servers sharing the database never lose a
change and a stale version gets 409. The table is created by the migration source `qcli-config` before the first read;
add `store.migration_source` to a database of this plugin to see it there.

```python
store = CliDatabaseConfigStore(postgres_connection(pool))
result = create_cli_server(CliServerOptions(config=CliServerConfigOptions(store=store)))
```

## Helpers for store authors

`table_exists`, `column_exists`, `rename_table`, `rename_column` and `adopt_legacy_table(db, legacy, table, columns)`
work on every dialect. `adopt_legacy_table` renames a previous release's table and its columns to the new names,
keeping the data.

## Tests

The runner and stats tests always run on SQLite. They also run on PostgreSQL, MySQL and SQL Server when these
variables point at empty databases the tests may use:

- `QCLI_TEST_POSTGRES`: a `postgres://` URL
- `QCLI_TEST_MYSQL`: a `mysql://` URL
- `QCLI_TEST_MSSQL`: `mssql://user:password@host:port/db`

## Terminal commands

`databases` (alias `db`), registered by `mount_plugin`. The database name may be left out when there is only one.

| Command | Permission | Does |
|---|---|---|
| `databases`, `databases list` | `databases:read` | Databases: dialect, size, applied, pending, problems |
| `databases stats <db>` | `databases:read` | Server version, uptime, size, connections, load counters, cache hits, largest tables |
| `databases migrations <db>` | `databases:read` | Every migration and its status |
| `databases runs <db> [-n 20] [--json]` | `databases:read` | Run history, newest first (1-100): start, direction, status, migrations run, trigger and who ran it, target, error (`GET /{db}/migrations/runs`) |
| `databases up <db> [--to <id>] [--dry-run] [--force]` | `databases:migrate` | Applies the pending migrations (`--dry-run` prints the SQL) |
| `databases down <db> --to <id\|base> [--dry-run]` | `databases:migrate` | Rolls back what came after `--to` |

```text
$ databases runs main -n 3
main (sqlite): 3 of 5 run(s)
Started             Dir  Status    Count Trigger
2026-11-25 10:02:00 down succeeded     1 terminal (root)  to 0002
2026-11-25 10:01:00 up   failed        0 api  no such table: Notes
2026-11-25 10:00:00 up   succeeded     2 startup
```

## License

MIT
