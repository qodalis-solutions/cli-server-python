"""The databases plugin's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``): one database from its
settings (dialect, connection a secret, migrations folder, apply on startup), shown with its size, load, largest
tables and migrations on the dashboard, in the terminal (``databases``), over MCP and at ``/api/v1/qcli/databases``.
It loads before the plugins that use it (``CLI_PLUGIN_LOAD_ORDER.CORE``) and is shared as the ``databases`` service.
In code, ``configure(builder)`` adds the host's own databases to the same plugin.  The driver (``asyncpg``,
``aiomysql``, ``pymssql``; ``sqlite3`` comes with Python) comes from the server's environment or the plugins folder.
Without a connection (or ``configure``) it needs configuration."""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .builder import CliDatabasesBuilder
from .connection import mssql_connection, mysql_connection, postgres_connection, sqlite_connection
from .runner import CliMigrationDatabaseOptions

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="databases", package="qodalis-cli-server-plugin-databases", title="Databases", icon="🗄️")

#: Before the plugins that keep their data in it.
order = CLI_PLUGIN_LOAD_ORDER.CORE

DIALECTS = ["postgres", "mysql", "mssql", "sqlite"]


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults; ``configure`` and ``audit_store`` from code)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="name", label="Name", description="The database in the API, the terminal and the dashboard",
                                   type="string", default="main",
                                   validation=CliConfigValidation(required=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"), **restart),
        CliConfigSettingDefinition(key="dialect", label="Dialect", type="enum", default="postgres",
                                   validation=CliConfigValidation(options=list(DIALECTS)), **restart),
        CliConfigSettingDefinition(
            key="connectionString", label="Connection",
            description="A connection string (postgresql://, mysql://, mssql://user:password@host:1433/db), or the SQLite file "
                        "(relative: the plugin data folder); kept as a secret",
            type="secret", required=True, **restart,
        ),
        CliConfigSettingDefinition(key="migrationsFolder", label="Migrations folder",
                                   description="NNNN_name.up.sql / .down.sql files (relative: the plugin data folder); empty: none",
                                   type="string", default="", validation=CliConfigValidation(max=500), **restart),
        CliConfigSettingDefinition(key="applyOnStartup", label="Apply on startup", description="Pending migrations run when the server starts",
                                   type="boolean", default=False, **restart),
    ]


def _driver(name: str) -> Any:
    try:
        return __import__(name)
    except ImportError as exc:
        raise RuntimeError(f"the {name} driver is not installed (pip install {name} next to the server)") from exc


async def open_connection(dialect: str, connection: str, data_dir: str) -> tuple[Any, Callable[[], Any]]:
    """A connection from a dialect and its connection string (or SQLite file), with its close."""
    if dialect == "sqlite":
        path = connection if connection == ":memory:" or os.path.isabs(connection) else os.path.join(data_dir, connection)
        conn = sqlite3.connect(path, check_same_thread=False)
        return sqlite_connection(conn), conn.close
    if dialect == "postgres":
        pool = await _driver("asyncpg").create_pool(connection)
        return postgres_connection(pool), pool.close
    url = urlparse(connection)
    if dialect == "mysql":
        aiomysql = _driver("aiomysql")
        from pymysql.constants import CLIENT  # aiomysql's dependency

        pool = await aiomysql.create_pool(
            host=url.hostname or "localhost", port=url.port or 3306, user=unquote(url.username or "root"),
            password=unquote(url.password or ""), db=url.path.lstrip("/") or None, autocommit=True,
            client_flag=CLIENT.MULTI_STATEMENTS,
        )

        async def close_mysql() -> None:
            pool.close()
            await pool.wait_closed()

        return mysql_connection(pool), close_mysql
    if dialect == "mssql":
        pymssql = _driver("pymssql")
        database = url.path.lstrip("/") or None
        return mssql_connection(lambda: pymssql.connect(
            server=url.hostname or "localhost", port=url.port or 1433, user=unquote(url.username or ""),
            password=unquote(url.password or ""), database=database or "",
        ), database), lambda: None
    raise ValueError(f"unknown dialect: {dialect}")


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="Databases", description="The database the databases plugin shows and migrates", settings=settings())

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    configure = context.options.get("configure")
    connection = text("connectionString")
    if not connection and not callable(configure):
        return context.needs_configuration(provider.missing_required())
    builder = CliDatabasesBuilder()
    audit_store = context.options.get("audit_store") or context.options.get("auditStore")
    if audit_store is not None:
        builder.set_audit_store(audit_store)
    if connection:
        opened, close = await open_connection(text("dialect") or "postgres", connection, context.data_dir)
        if context.restart is not None and hasattr(context.restart, "on_shutdown"):
            context.restart.on_shutdown("databases connection", close)
        name = text("name") or "main"
        builder.add_database(name, CliMigrationDatabaseOptions(opened, apply_on_startup=provider.value("settings", "applyOnStartup") is True))
        folder = text("migrationsFolder")
        if folder:
            builder.add_sql_folder(name, folder if os.path.isabs(folder) else os.path.join(context.data_dir, folder))
    if callable(configure):
        configure(builder)
    result = builder.build()
    await result.apply_on_startup()
    context.services.set("databases", result.databases)
    return result
