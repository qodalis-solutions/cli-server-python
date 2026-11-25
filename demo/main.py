"""Demo CLI server with sample processors.

This demo showcases core CLI commands, the weather plugin, the jobs plugin,
the admin dashboard plugin, and the pluggable file-storage provider system.
By default the server uses an in-memory file store, but you can switch to any
of the available providers by uncommenting the relevant section below.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)
logger = logging.getLogger(__name__)

# Allow importing from the project root so ``plugins`` is accessible.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import APIRouter, Depends

from qodalis_cli import (
    CliMcpOptions,
    CliPermission,
    CliServerConfigOptions,
    CliServerOptions,
    CliServerRestartOptions,
    CliShellOptions,
    create_cli_server,
    CliHttpCommandProcessor,
    FileSystemOptions,
    InMemoryFileStorageProvider,
    OsFileStorageProvider,
    OsProviderOptions,
    install_log_redaction,
)
from qodalis_cli_server_abstractions import DataExplorerProviderOptions
from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
    JsonFileConfigStore,
)
from qodalis_cli_server_abstractions.jobs import CliJobOptions

from qodalis_cli_jobs import CliJobsBuilder
from qodalis_cli_admin import CliAdminBuilder, AdminBuildDeps
from qodalis_cli_users import CliUsersBuilder, QcliCaller
from qodalis_cli_public_api import CliPublicApiBuilder, PublicApiQuota
from qodalis_cli_docs import CliDocsBuilder
from qodalis_cli_portal import CliPortalBuilder

from demo_module import DemoModule

from plugins.weather import WeatherModule
from plugins.aws.qodalis_cli_aws import AwsModule
from qodalis_cli_data_explorer_sql import SqlDataExplorerProvider
from qodalis_cli_data_explorer_postgres import PostgresDataExplorerProvider
from qodalis_cli_data_explorer_mysql import MysqlDataExplorerProvider
from qodalis_cli_data_explorer_mssql import MssqlDataExplorerProvider
from qodalis_cli_data_explorer_redis import RedisDataExplorerProvider
from qodalis_cli_data_explorer_elasticsearch import ElasticsearchDataExplorerProvider
from qodalis_cli_server_abstractions import (
    DataExplorerLanguage,
    DataExplorerOutputFormat,
    DataExplorerTemplate,
)
from jobs import SampleHealthCheckJob

# ---------------------------------------------------------------------------
# File storage providers
# ---------------------------------------------------------------------------
# The CLI server ships with a pluggable IFileStorageProvider system.  Pick one
# of the providers below and pass it to ``builder.set_file_storage_provider()``
# inside the configure callback.
#
# 1) In-memory (default — files are lost when the server restarts):
#
#    provider = InMemoryFileStorageProvider()
#
# 2) OS / local filesystem (reads & writes real files; paths must be
#    explicitly allowed):
#
#    provider = OsFileStorageProvider(
#        OsProviderOptions(allowed_paths=["/tmp", "/app/data"])
#    )
#
# 3) JSON file (all files serialised into a single JSON file):
#
#    import importlib
#    _json_mod = importlib.import_module("plugins.filesystem-json")
#    provider = _json_mod.JsonFileStorageProvider(
#        _json_mod.JsonFileProviderOptions(file_path="./data.json")
#    )
#
# 4) SQLite (files stored in a local SQLite database):
#
#    import importlib
#    _sqlite_mod = importlib.import_module("plugins.filesystem-sqlite")
#    provider = _sqlite_mod.SqliteFileStorageProvider(
#        _sqlite_mod.SqliteProviderOptions(db_path="./files.db")
#    )
#
# 5) AWS S3 (files stored in an S3 bucket — requires boto3):
#
#    import importlib
#    _s3_mod = importlib.import_module("plugins.filesystem-s3")
#    provider = _s3_mod.S3FileStorageProvider(
#        _s3_mod.S3ProviderOptions(bucket="my-cli-files", region="us-east-1")
#    )
# ---------------------------------------------------------------------------

# Choose a provider (default: in-memory).
file_storage_provider = InMemoryFileStorageProvider()


DEMO_DIR = os.path.dirname(os.path.abspath(__file__))


def create_demo_server():  # noqa: ANN201
    """The demo server (``CliServerResult``), without running it."""

    # -----------------------------------------------------------------------
    # Config stores: settings changed from the dashboard's Config page,
    # `config set` or MCP `config_set` are kept and applied again at the next
    # start.  Each provider may keep its settings in a store of its own:
    #
    #   - the default store, for every provider without one: a JSON file
    #     (QCLI_DEMO_CONFIG).  To keep them in a section of the app's own
    #     settings file instead: JsonFileConfigStore("config/app.json", section="qcli")
    #   - `jobs` (history retention): the demo's SQLite database when it has one
    #     (QCLI_DEMO_DB, a file path), table QcliConfig, set by id with
    #     `stores` (the host's choice wins over the plugin's); else the default store
    #   - `demo` (the app's own settings below): Redis when QCLI_DEMO_REDIS_URL
    #     is set (redis://localhost:6379/0), through the provider's own `store`;
    #     else the default store
    #
    # `config list` and GET /api/v1/qcli/config show the store of every provider.
    # -----------------------------------------------------------------------
    import tempfile

    config_store = JsonFileConfigStore(
        os.environ.get("QCLI_DEMO_CONFIG") or os.path.join(tempfile.gettempdir(), "qcli-demo-config.json"),
    )
    config_stores: dict = {}
    demo_db_path = os.environ.get("QCLI_DEMO_DB")
    if demo_db_path:
        import sqlite3

        from qodalis_cli_databases import CliDatabaseConfigStore, sqlite_connection

        # The table is created by the store itself at startup (config.ready())
        config_stores["jobs"] = CliDatabaseConfigStore(
            sqlite_connection(sqlite3.connect(demo_db_path, check_same_thread=False)),
            database=os.path.basename(demo_db_path),
        )
    redis_config_store = None
    if os.environ.get("QCLI_DEMO_REDIS_URL"):
        from qodalis_cli_config_redis import CliRedisConfigStore

        redis_config_store = CliRedisConfigStore(url=os.environ["QCLI_DEMO_REDIS_URL"])
    # A host app's own settings: what the demo's greeting says, and a secret it never shows
    greeting: dict = {"text": "Hello from the Qodalis CLI demo", "api_key": None}
    demo_config = CliStoredConfigProvider(
        id="demo",
        title="Demo app",
        description="The demo host application's own settings",
        # Its own store: Redis with QCLI_DEMO_REDIS_URL, else (None) the default store
        store=redis_config_store,
        sections=[CliConfigSectionDefinition(id="greeting", title="Greeting", settings=[
            CliConfigSettingDefinition(
                key="text", label="Greeting", type="string", default=greeting["text"],
                validation=CliConfigValidation(required=True, max=120),
                apply=lambda v: greeting.__setitem__("text", str(v)),
            ),
            CliConfigSettingDefinition(
                key="apiKey", label="Partner API key", description="Never shown, only whether it is set",
                type="secret", apply=lambda v: greeting.__setitem__("api_key", v),
            ),
        ])],
    )

    def configure(builder):  # noqa: ANN001
        # Host permission of the public API (a host token is created with it).
        builder.add_permissions([
            CliPermission(name="demo:read", description="Read the demo's public API", group="Demo"),
        ])
        builder.add_config_provider(demo_config)
        builder \
            .add_module(DemoModule()) \
            .add_processor(CliHttpCommandProcessor()) \
            .add_module(WeatherModule()) \
            .add_module(AwsModule()) \
            .add_data_explorer_provider(
                SqlDataExplorerProvider(":memory:"),
                DataExplorerProviderOptions(
                    name="sql",
                    description="In-memory SQLite database for ad-hoc queries",
                ),
            ) \
            .set_file_storage_provider(file_storage_provider) \
            .add_filesystem(FileSystemOptions(allowed_paths=["/tmp", "/app", "/home"]))

        # -----------------------------------------------------------
        # Data Explorer — MongoDB Provider
        # -----------------------------------------------------------
        mongo_conn = os.environ.get("MONGO_CONNECTION_STRING")
        if mongo_conn:
            from qodalis_cli_data_explorer_mongo import MongoDataExplorerProvider
            builder.add_data_explorer_provider(
                MongoDataExplorerProvider(mongo_conn, "demo"),
                DataExplorerProviderOptions(
                    name="demo-mongo",
                    description="Demo MongoDB database",
                    language=DataExplorerLanguage.JSON,
                    default_output_format=DataExplorerOutputFormat.JSON,
                    templates=[
                        DataExplorerTemplate("show_collections", "show collections", "List all collections"),
                        DataExplorerTemplate("find_all", "db.users.find({})", "Find all documents in users collection"),
                    ],
                ),
            )

        # -----------------------------------------------------------
        # Data Explorer — PostgreSQL Provider
        # -----------------------------------------------------------
        pg_conn = os.environ.get("POSTGRES_CONNECTION_STRING")
        if pg_conn:
            builder.add_data_explorer_provider(
                PostgresDataExplorerProvider(pg_conn),
                DataExplorerProviderOptions(
                    name="demo-postgres",
                    description="Demo PostgreSQL database",
                    language=DataExplorerLanguage.SQL,
                    default_output_format=DataExplorerOutputFormat.TABLE,
                ),
            )

        # -----------------------------------------------------------
        # Data Explorer — MySQL Provider
        # -----------------------------------------------------------
        mysql_conn = os.environ.get("MYSQL_CONNECTION_STRING")
        if mysql_conn:
            builder.add_data_explorer_provider(
                MysqlDataExplorerProvider(mysql_conn),
                DataExplorerProviderOptions(
                    name="demo-mysql",
                    description="Demo MySQL database",
                    language=DataExplorerLanguage.SQL,
                    default_output_format=DataExplorerOutputFormat.TABLE,
                ),
            )

        # -----------------------------------------------------------
        # Data Explorer — MS SQL Provider
        # -----------------------------------------------------------
        mssql_conn = os.environ.get("MSSQL_CONNECTION_STRING")
        if mssql_conn:
            builder.add_data_explorer_provider(
                MssqlDataExplorerProvider(mssql_conn),
                DataExplorerProviderOptions(
                    name="demo-mssql",
                    description="Demo MS SQL Server database",
                    language=DataExplorerLanguage.SQL,
                    default_output_format=DataExplorerOutputFormat.TABLE,
                ),
            )

        # -----------------------------------------------------------
        # Data Explorer — Redis Provider
        # -----------------------------------------------------------
        redis_conn = os.environ.get("REDIS_CONNECTION_STRING")
        if redis_conn:
            builder.add_data_explorer_provider(
                RedisDataExplorerProvider(redis_conn),
                DataExplorerProviderOptions(
                    name="demo-redis",
                    description="Demo Redis instance",
                    language=DataExplorerLanguage.REDIS,
                    default_output_format=DataExplorerOutputFormat.TABLE,
                ),
            )

        # -----------------------------------------------------------
        # Data Explorer — Elasticsearch Provider
        # -----------------------------------------------------------
        es_node = os.environ.get("ELASTICSEARCH_NODE")
        if es_node:
            builder.add_data_explorer_provider(
                ElasticsearchDataExplorerProvider(es_node),
                DataExplorerProviderOptions(
                    name="demo-elasticsearch",
                    description="Demo Elasticsearch cluster",
                    language=DataExplorerLanguage.ELASTICSEARCH,
                    default_output_format=DataExplorerOutputFormat.JSON,
                ),
            )

    # Local dev origins (docs/demos on 4200, 4300-4303).  Used for CORS and
    # the WebSocket Origin check.  The server's own origin (the bundled admin
    # dashboard) needs no entry: same-origin requests are always allowed.
    dev_ports = [4200, 4300, 4301, 4302, 4303]
    allowed_origins = [
        f"http://{h}:{p}" for p in dev_ports for h in ("localhost", "127.0.0.1")
    ]

    result = create_cli_server(
        CliServerOptions(
            configure=configure,
            allowed_origins=allowed_origins,
            config=CliServerConfigOptions(store=config_store, stores=config_stores),
            # Restart from the dashboard or `admin restart --yes`: the demo runs bare (python demo/main.py, nohup),
            # with no supervisor, so it runs itself again ("self": the same command, environment, working directory
            # and output).  Under Docker, systemd or PM2 leave the mode out ("auto" exits with 75 for the supervisor
            # to start it again).
            restart=CliServerRestartOptions(mode="self"),
            # Opt-in remote shell.  The users plugin is mounted below, so the
            # shell runs in "users" mode: a WebSocket ticket of a session with
            # the shell:execute permission (root has '*').  Without the users
            # plugin, set shell=CliShellOptions(token=os.environ["QCLI_SHELL_TOKEN"])
            # instead; with neither, the shell would stay locked (4401).
            enable_shell=True,
            shell=CliShellOptions(idle_timeout=15 * 60, max_sessions_per_user=3),
            # MCP endpoint (POST /api/qcli/mcp): commands, files, data explorer
            # and jobs as MCP tools.  The users plugin authenticates it — use an
            # API token (`token create mcp` in the CLI, or POST /api/v1/qcli/tokens):
            #   claude mcp add --transport http qcli http://localhost:8048/api/qcli/mcp \
            #     --header "Authorization: Bearer <token>"
            mcp=CliMcpOptions(
                instructions="Qodalis CLI demo server: run CLI commands, browse files, "
                             "query data sources and manage background jobs.",
            ),
        )
    )

    # Users plugin: protects every route and socket of this server (login,
    # version discovery and health stay public).  Seeded and started in the
    # app lifespan below.  The demo keeps the well-known 'admin' password.
    # It mounts the audit plugin too (the audit log, /api/v1/qcli/audit).
    users_plugin = CliUsersBuilder().set_default_admin_password("admin").build()
    result.mount_plugin(users_plugin)

    # Build the jobs plugin
    jobs_plugin = (
        CliJobsBuilder()
        .add_job(SampleHealthCheckJob(), CliJobOptions(
            name="health-check",
            description="Periodic health check",
            group="monitoring",
            tags=["health", "demo"],
            interval="30s",
        ))
        .build()  # job:* events go to the server's event bus once mounted
    )

    result.mount_plugin(jobs_plugin)

    # Build the admin plugin — the dashboard logs in through the users plugin
    # (requires the admin:access permission; root has '*').
    admin_plugin = (
        CliAdminBuilder()
        .set_users_plugin(users_plugin)
        .build(AdminBuildDeps(
            registry=result.registry,
            event_socket_manager=result.event_socket_manager,
            builder=result.builder,
            # log:entry events go to the server's event bus (audience logs:read) once mounted
            enabled_features=["jobs"],
        ))
    )

    result.mount_plugin(admin_plugin)

    # Register the module registry as a processor filter so that toggling
    # a plugin off in the admin dashboard actually blocks command execution.
    if admin_plugin.module_registry and result.executor:
        result.executor.add_filter(admin_plugin.module_registry)

    # Wire the jobs scheduler and users plugin lifecycles into the app lifespan
    original_lifespan = result.app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_plugins(app: object) -> AsyncIterator[None]:
        await jobs_plugin.scheduler.start()
        await users_plugin.startup()
        try:
            async with original_lifespan(app) as state:  # type: ignore[arg-type]
                yield state
        finally:
            await users_plugin.shutdown()
            await jobs_plugin.scheduler.stop()
            if redis_config_store is not None:
                await redis_config_store.close()

    result.app.router.lifespan_context = lifespan_with_plugins

    # A restart's graceful shutdown stops the jobs and disposes the mounted plugins itself, then runs the server
    # again before the lifespan above unwinds: the users plugin and the Redis config store are closed as its steps
    result.restart.on_shutdown("users plugin", users_plugin.shutdown)
    if redis_config_store is not None:
        result.restart.on_shutdown("redis config store", redis_config_store.close)

    # -----------------------------------------------------------------------
    # Public API, guides and the portal
    # -----------------------------------------------------------------------
    # A public route guarded by host API tokens (create one for a user with
    # users_plugin.api_tokens.create(..., ApiTokenCreateOptions(audience="host",
    # permissions=["demo:read"]))), 1000 requests a day and 60 a minute per
    # account; its OpenAPI document at /qcli/api/openapi.json, Swagger UI at
    # /qcli/api/swagger, guides at /qcli/docs/pages and the portal at
    # /qcli/portal.
    public = APIRouter()
    public_api = (
        CliPublicApiBuilder()
        .set_users(users_plugin)
        .set_openapi(public)
        .set_title("Demo API")
        .set_quota(lambda caller: PublicApiQuota(requests_per_day=1000, requests_per_minute=60))
        .build()
    )

    @public.get("/v1/demo/time", tags=["Demo"])
    async def demo_time(caller: QcliCaller = Depends(public_api.guard("demo:read"))) -> dict:
        from datetime import datetime, timezone

        return {"utc": datetime.now(timezone.utc).isoformat(), "account": caller.user_name}

    result.app.include_router(public)
    result.mount_plugin(public_api)

    docs_plugin = (
        CliDocsBuilder()
        .set_name("Qodalis CLI demo")
        .add_folder(os.path.join(DEMO_DIR, "docs"))
        .build()
    )
    result.mount_plugin(docs_plugin)
    portal_plugin = (
        CliPortalBuilder()
        .set_name("Qodalis CLI demo")
        .add_section(docs_plugin)
        .add_section(public_api)
        .build()
    )
    result.mount_plugin(portal_plugin)
    result.demo_admin_plugin = admin_plugin  # type: ignore[attr-defined]
    result.demo_portal_plugin = portal_plugin  # type: ignore[attr-defined]
    return result


def main() -> None:
    port = int(os.environ.get("PORT", "8048"))
    host = os.environ.get("HOST", "0.0.0.0")
    result = create_demo_server()
    admin_plugin = result.demo_admin_plugin  # type: ignore[attr-defined]

    logger.info("Qodalis CLI Demo Server (Python) running on http://%s:%d", host, port)
    logger.info("  API: http://%s:%d/api/qcli", host, port)
    logger.info("  Users API: http://%s:%d/api/v1/qcli/users", host, port)
    logger.info("  Audit API: http://%s:%d/api/v1/qcli/audit", host, port)
    logger.info("  Admin API: http://%s:%d/api/v1/qcli/admin", host, port)
    if admin_plugin.dashboard_app:
        logger.info("  Admin Dashboard: http://%s:%d/qcli/admin", host, port)
    logger.info("  MCP: http://%s:%d/api/qcli/mcp", host, port)
    logger.info("  Public API: http://%s:%d/qcli/api/swagger", host, port)
    if result.demo_portal_plugin.site_dir:  # type: ignore[attr-defined]
        logger.info("  Portal: http://%s:%d/qcli/portal", host, port)
    logger.info("  WebSocket: ws://%s:%d/ws/qcli/events", host, port)
    logger.info("  File storage: %s", type(file_storage_provider).__name__)

    # uvicorn logs paths with query strings (tickets, tokens, shell commands).
    install_log_redaction()
    uvicorn.run(result.app, host=host, port=port)


if __name__ == "__main__":
    main()
