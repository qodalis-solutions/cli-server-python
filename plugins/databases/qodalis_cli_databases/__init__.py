"""Qodalis CLI databases plugin: the server's PostgreSQL, MySQL/MariaDB, SQL Server and SQLite databases, their
stats (size, connections, load, largest tables) and their migrations (versioned schema changes applied under a
lock, recorded, and shown, run and rolled back from the dashboard, the terminal, MCP and the API)."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🗄️"

from .builder import (
    DATABASES_PERMISSION_ENTRIES,
    DATABASES_PREFIX,
    CliDatabasesBuilder,
    CliDatabasesPlugin,
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
from .controller import create_databases_router
from .commands import CliDatabasesCommandProcessor
from .dialects import (
    adopt_legacy_table,
    column_exists,
    quote_ident,
    quote_table,
    rename_column,
    rename_table,
    table_exists,
)
from .databases import MIGRATION_RUN_EVENT, DATABASES_READ, DATABASES_MIGRATE, CliDatabases
from .stats import collect_size, collect_stats
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
    "DATABASES_PERMISSION_ENTRIES",
    "DATABASES_PREFIX",
    "CliDatabasesBuilder",
    "CliDatabasesPlugin",
    "SQL_DIALECTS",
    "CliSqlConnection",
    "CliSqlDialect",
    "CliSqlSession",
    "mssql_connection",
    "mysql_connection",
    "postgres_connection",
    "sqlite_connection",
    "create_databases_router",
    "CliDatabasesCommandProcessor",
    "adopt_legacy_table",
    "column_exists",
    "quote_ident",
    "quote_table",
    "rename_column",
    "rename_table",
    "table_exists",
    "MIGRATION_RUN_EVENT",
    "DATABASES_READ",
    "DATABASES_MIGRATE",
    "CliDatabases",
    "collect_size",
    "collect_stats",
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
