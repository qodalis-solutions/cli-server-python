"""Qodalis CLI migrations plugin: versioned schema changes of PostgreSQL, MySQL/MariaDB, SQL Server and SQLite
databases, applied under a lock, recorded, and shown, run and rolled back from the dashboard, the terminal, MCP
and the API."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🗄️"

from .builder import (
    MIGRATIONS_PERMISSION_ENTRIES,
    MIGRATIONS_PREFIX,
    CliMigrationsBuilder,
    CliMigrationsPlugin,
)
from .connection import (
    SQL_DIALECTS,
    CliSqlConnection,
    CliSqlDialect,
    CliSqlSession,
    mssql_connection,
    mysql_connection,
    postgres_connection,
    sqlite_connection,
)
from .controller import create_migrations_router
from .commands import CliMigrationsCommandProcessor
from .dialects import (
    adopt_legacy_table,
    column_exists,
    quote_ident,
    quote_table,
    rename_column,
    rename_table,
    table_exists,
)
from .migrations import MIGRATION_RUN_EVENT, MIGRATIONS_READ, MIGRATIONS_RUN, CliMigrations
from .runner import (
    CliMigrationDatabaseOptions,
    CliMigrationError,
    CliMigrationRun,
    CliMigrationRunner,
    CliMigrationRunOptions,
    CliMigrationState,
    CliMigrationStatus,
    CliMigrationTrigger,
)
from .sources import CliMigration, CliMigrationSource, CliMigrationStep, CodeSource, SqlFolderSource, code_source, sql_folder_source

__all__ = [
    "MIGRATIONS_PERMISSION_ENTRIES",
    "MIGRATIONS_PREFIX",
    "CliMigrationsBuilder",
    "CliMigrationsPlugin",
    "SQL_DIALECTS",
    "CliSqlConnection",
    "CliSqlDialect",
    "CliSqlSession",
    "mssql_connection",
    "mysql_connection",
    "postgres_connection",
    "sqlite_connection",
    "create_migrations_router",
    "CliMigrationsCommandProcessor",
    "adopt_legacy_table",
    "column_exists",
    "quote_ident",
    "quote_table",
    "rename_column",
    "rename_table",
    "table_exists",
    "MIGRATION_RUN_EVENT",
    "MIGRATIONS_READ",
    "MIGRATIONS_RUN",
    "CliMigrations",
    "CliMigrationDatabaseOptions",
    "CliMigrationError",
    "CliMigrationRun",
    "CliMigrationRunner",
    "CliMigrationRunOptions",
    "CliMigrationState",
    "CliMigrationStatus",
    "CliMigrationTrigger",
    "CliMigration",
    "CliMigrationSource",
    "CliMigrationStep",
    "CodeSource",
    "SqlFolderSource",
    "code_source",
    "sql_folder_source",
]
