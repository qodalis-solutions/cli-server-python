"""Fluent builder of the databases plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from fastapi import APIRouter

from qodalis_cli_server_abstractions import CliModule, CliRoutePermission, IAuditStore, ICliCommandProcessor

from .commands import CliDatabasesCommandProcessor
from .dashboard_page import DATABASES_DASHBOARD_PREFIX, find_databases_page, databases_page
from .controller import create_databases_router
from .databases import DATABASES_READ, DATABASES_MIGRATE, CliDatabases
from .runner import CliMigrationDatabaseOptions, CliMigrationRun, CliMigrationRunner, CliMigrationRunOptions
from .sources import CliMigration, CliMigrationSource, code_source, sql_folder_source

DATABASES_PREFIX = "/api/v1/qcli/databases"

#: The permissions the plugin adds to the server's catalog.
DATABASES_PERMISSION_ENTRIES: list[dict[str, str]] = [
    {"name": DATABASES_READ, "description": "See databases, their stats and migrations", "group": "Databases"},
    {"name": DATABASES_MIGRATE, "description": "Apply and roll back database migrations", "group": "Databases"},
]

_DATABASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class _DatabasesModule(CliModule):
    def __init__(self, processor: ICliCommandProcessor) -> None:
        self._processors = [processor]

    @property
    def name(self) -> str:
        return "databases"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Databases: stats, migrations (status, apply, roll back)"

    @property
    def icon(self) -> str:
        return "🗄️"

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        return self._processors


@dataclass
class CliDatabasesPlugin:
    """The built plugin: mount it with ``result.mount_plugin(plugin)``."""

    prefix: str
    router: APIRouter
    databases: CliDatabases
    #: The ``databases`` terminal commands, registered when mounted.
    module: CliModule
    #: ``databases:read`` for GET, ``databases:migrate`` for migration runs (enforced on a protected server).
    route_permissions: list[CliRoutePermission] = field(default_factory=list)
    #: ``databases:read`` and ``databases:migrate``, added to the server's permission catalog when mounted.
    permissions: list[dict[str, str]] = field(default_factory=list)
    #: The dashboard's Databases page (``/qcli/databases``), when its build is there.
    dashboard_prefix: str | None = None
    dashboard_app: Any = None
    dashboard_extensions: list[dict[str, Any]] = field(default_factory=list)
    _startup: list[str] = field(default_factory=list, repr=False)

    @property
    def mcp_providers(self) -> list[Any]:
        """The ``databases_*`` and ``migrations_*`` MCP tools, served when the server's MCP endpoint is on."""
        try:
            from .mcp_tools import DatabasesMcpToolProvider
        except ImportError:  # without the server package
            return []
        return [DatabasesMcpToolProvider(self.databases)]

    def on_mount(self, server: Any) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: migration runs publish ``databases:migration-run`` on the server's
        event bus."""
        bus = getattr(server, "event_bus", None)
        if self.databases.event_bus is None and bus is not None:
            self.databases.event_bus = bus

    async def apply_on_startup(self) -> list[CliMigrationRun]:
        """Applies the pending migrations of the databases marked ``apply_on_startup``, one after another,
        waiting for another process's run; raises ``RuntimeError`` with the first failure.  Await it before
        the server serves requests (e.g. in the app's lifespan)."""
        runs: list[CliMigrationRun] = []
        for name in self._startup:
            run = await self.databases.run(name, "up", CliMigrationRunOptions(trigger="startup", wait=True))
            runs.append(run)
            if run.status == "failed":
                raise RuntimeError(f"migrations of {name} failed: {run.error}")
        return runs


class CliDatabasesBuilder:
    """The databases plugin: the server's relational databases (PostgreSQL, MySQL, SQL Server, SQLite), their
    stats (size, connections, load, largest tables) and their migrations: versioned schema changes applied under
    a lock, recorded in ``QcliMigrations`` / ``QcliMigrationRuns``, and shown, run and rolled back from the
    dashboard, the terminal (``databases``), MCP and the API at ``/api/v1/qcli/databases``.

    Example::

        databases = (
            CliDatabasesBuilder()
            .add_database("main", CliMigrationDatabaseOptions(postgres_connection(pool), apply_on_startup=True))
            .add_sql_folder("main", "./migrations")
            .build()
        )
        await databases.apply_on_startup()
        result.mount_plugin(databases)
    """

    def __init__(self) -> None:
        self._databases: list[tuple[str, CliMigrationDatabaseOptions, list[CliMigrationSource]]] = []
        self._audit_store: IAuditStore | None = None
        self._dashboard_dir: str | bool | None = None

    def add_database(self, name: str, options: CliMigrationDatabaseOptions) -> "CliDatabasesBuilder":
        """Registers a database (its name is used in the API, the terminal and the dashboard)."""
        if not _DATABASE_NAME.match(name or ""):
            raise ValueError(f"invalid database name: {name}")
        if any(n == name for n, _, _ in self._databases):
            raise ValueError(f"migration database already added: {name}")
        self._databases.append((name, options, []))
        return self

    def add_source(self, database: str, source: CliMigrationSource) -> "CliDatabasesBuilder":
        """Adds a source of migrations to a database; sources apply in the order they were added."""
        for name, _, sources in self._databases:
            if name == database:
                sources.append(source)
                return self
        raise ValueError(f"unknown migration database: {database}")

    def add_sql_folder(self, database: str, directory: str, source_name: str = "app") -> "CliDatabasesBuilder":
        """Migrations from ``NNNN_name.up.sql`` / ``.down.sql`` files in ``directory``."""
        return self.add_source(database, sql_folder_source(directory, source_name))

    def add_migrations(self, database: str, source_name: str, migrations: Sequence[CliMigration]) -> "CliDatabasesBuilder":
        """Migrations given in code, as source ``source_name`` (how plugins ship their schema)."""
        return self.add_source(database, code_source(source_name, migrations))

    def set_audit_store(self, store: IAuditStore) -> "CliDatabasesBuilder":
        """Writes every run to the audit log (``migrations.up`` / ``migrations.down``)."""
        self._audit_store = store
        return self

    def set_dashboard_dir(self, directory: str | bool) -> "CliDatabasesBuilder":
        """Where the dashboard's Databases page is (``@qodalis/cli-dashboard-module-databases``'s build); by
        default the copy inside the package.  ``False`` leaves the page out."""
        self._dashboard_dir = directory
        return self

    def build(self) -> CliDatabasesPlugin:
        databases = CliDatabases()
        for name, options, sources in self._databases:
            runner = CliMigrationRunner(name, options)
            for source in sources:
                runner.add_source(source)
            databases.add(runner)
        databases.audit_store = self._audit_store
        page_dir = None if self._dashboard_dir is False else find_databases_page(self._dashboard_dir or None)  # type: ignore[arg-type]
        page = databases_page(page_dir) if page_dir else None
        return CliDatabasesPlugin(
            prefix=DATABASES_PREFIX,
            router=create_databases_router(databases),
            databases=databases,
            module=_DatabasesModule(CliDatabasesCommandProcessor(databases)),
            route_permissions=[CliRoutePermission(DATABASES_PREFIX, DATABASES_READ, DATABASES_MIGRATE)],
            permissions=list(DATABASES_PERMISSION_ENTRIES),
            dashboard_prefix=DATABASES_DASHBOARD_PREFIX if page else None,
            dashboard_app=page[0] if page else None,
            dashboard_extensions=[page[1]] if page else [],
            _startup=[name for name, options, _ in self._databases if options.apply_on_startup],
        )
