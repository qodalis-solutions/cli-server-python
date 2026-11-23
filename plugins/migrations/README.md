# qodalis-cli-server-plugin-migrations

Database migrations for the Qodalis CLI server: versioned schema changes of PostgreSQL, MySQL/MariaDB, SQL Server
and SQLite databases, applied under a lock, recorded with who ran them, when and how long they took, and shown, run
and rolled back from the admin dashboard, the web terminal (`migrations`), MCP and the API.

```bash
pip install qodalis-cli-server-plugin-migrations[postgres]   # or [mysql], [mssql]; SQLite needs nothing
```

## Use

```python
from contextlib import asynccontextmanager

import asyncpg
from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_migrations import CliMigrationDatabaseOptions, CliMigrationsBuilder, postgres_connection


@asynccontextmanager
async def lifespan(app):
    pool = await asyncpg.create_pool(DATABASE_URL)
    migrations = (
        CliMigrationsBuilder()
        .add_database("main", CliMigrationDatabaseOptions(postgres_connection(pool), apply_on_startup=True))
        .add_sql_folder("main", "./migrations")
        .build()
    )
    await migrations.apply_on_startup()  # pending migrations before serving; a failure stops startup
    result.mount_plugin(migrations)
    yield
```

Connections: `postgres_connection(pool)` (`asyncpg` pool), `mysql_connection(pool)` (`aiomysql` pool created with
`client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS`), `mssql_connection(lambda: pymssql.connect(...))` (a
connection per session, on a thread of its own), `sqlite_connection(conn)` (`sqlite3`; the connection is switched to
autocommit, migrations open their own transactions). The plugin needs none of the drivers itself.

## Migrations

- **A folder of SQL files**: `0001_create_users.up.sql`, with an optional `0001_create_users.down.sql` to roll it
  back. A file starting with `-- qcli:no-transaction` runs outside a transaction (e.g. `CREATE INDEX CONCURRENTLY`).
- **Code**: `add_migrations("main", "billing", [CliMigration("1", "create invoices", "CREATE TABLE ...", "DROP TABLE ...")])`.
  `up`/`down` can be SQL, SQL per dialect (`{"postgres": "...", "sqlite": "..."}`) or a coroutine function given
  the session (`await db.query(sql, params)`, `await db.exec(script)`). This is how plugins ship their schema.

A database can have several sources; they apply in the order they were added. Released migrations are never edited
or reordered: add a new one instead. Each migration runs in its own transaction together with its history row
(MySQL commits DDL statements on its own, so a failing MySQL migration may leave a table behind).

## Status, runs and safety

- `QcliMigrations` holds the applied migrations (source, id, name, checksum, applied at/by, duration) and
  `QcliMigrationRuns` every run (direction, target, trigger, who, start/end, status, error, what it applied). The
  names follow the plugin table names (`table_prefix`, `schema`, per-table `tables` overrides on
  `CliMigrationDatabaseOptions`).
- A migration is `applied`, `pending`, `changed` (its SQL differs from what was applied), `missing` (applied, no
  longer in its source) or `failed` (the last run stopped at it). Runs are refused while one is `changed` or
  `missing`, unless `force`.
- One run at a time: a lock per database (`pg_advisory_lock`, `GET_LOCK`, `sp_getapplock`) across processes, so two
  servers starting together apply once. The API, terminal and MCP answer busy (409); `apply_on_startup` waits.
- Every run publishes `migrations:run` (audience `migrations:read`) and, with `set_audit_store(store)`, writes
  `migrations.up` / `migrations.down` to the audit log.

## API, terminal and MCP

| | |
|---|---|
| `GET /api/v1/qcli/migrations` | databases: applied, pending, problems, last run |
| `GET /api/v1/qcli/migrations/{db}` | its migrations and their status |
| `GET /api/v1/qcli/migrations/{db}/runs?page=&size=` | run history, newest first |
| `GET /api/v1/qcli/migrations/{db}/sql?direction=up\|down&to=` | the SQL a run would execute (dry run) |
| `POST /api/v1/qcli/migrations/{db}/up` `{ to?, force? }` | apply pending migrations |
| `POST /api/v1/qcli/migrations/{db}/down` `{ to }` | roll back to `to` (kept; `base` for everything) |

Permissions on a protected server: `migrations:read` for reading, `migrations:run` for up and down. Terminal:
`migrations`, `migrations status <db>`, `migrations up <db> [--to <id>] [--dry-run] [--force]`,
`migrations down <db> --to <id> [--dry-run]`. With the server's MCP endpoint on, AI clients get
`migrations_status`, `migrations_runs`, `migrations_sql` (`migrations:read`), `migrations_up` and
`migrations_down` (`migrations:run`); their runs are recorded with trigger `mcp`.

## Helpers for store authors

`table_exists`, `column_exists`, `rename_table`, `rename_column` and `adopt_legacy_table(db, legacy, table, columns)`
(renames a previous release's table and its columns to the new names, keeping the data) work on every dialect.

## Tests

The runner tests run on SQLite, and on PostgreSQL, MySQL and SQL Server when `QCLI_TEST_POSTGRES`
(`postgres://` URL), `QCLI_TEST_MYSQL` (`mysql://` URL) and `QCLI_TEST_MSSQL` (`mssql://user:password@host:port/db`)
point at empty databases the tests may use.

## License

MIT
