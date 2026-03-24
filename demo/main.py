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

from qodalis_cli import (
    CliServerOptions,
    create_cli_server,
    CliSystemCommandProcessor,
    CliHttpCommandProcessor,
    CliHashCommandProcessor,
    CliBase64CommandProcessor,
    FileSystemOptions,
    InMemoryFileStorageProvider,
    OsFileStorageProvider,
    OsProviderOptions,
)
from qodalis_cli_server_abstractions import DataExplorerProviderOptions
from qodalis_cli_server_abstractions.jobs import CliJobOptions

from qodalis_cli_jobs import CliJobsBuilder
from qodalis_cli_admin import CliAdminBuilder, AdminBuildDeps

from processors import (
    CliEchoCommandProcessor,
    CliHelloCommandProcessor,
    CliMathCommandProcessor,
    CliStatusCommandProcessor,
    CliTimeCommandProcessor,
)

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


def main() -> None:
    port = int(os.environ.get("PORT", "8048"))
    host = os.environ.get("HOST", "0.0.0.0")

    def configure(builder):  # noqa: ANN001
        builder \
            .add_processor(CliEchoCommandProcessor()) \
            .add_processor(CliStatusCommandProcessor()) \
            .add_processor(CliTimeCommandProcessor()) \
            .add_processor(CliHelloCommandProcessor()) \
            .add_processor(CliMathCommandProcessor()) \
            .add_processor(CliSystemCommandProcessor()) \
            .add_processor(CliHttpCommandProcessor()) \
            .add_processor(CliHashCommandProcessor()) \
            .add_processor(CliBase64CommandProcessor()) \
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

    result = create_cli_server(
        CliServerOptions(configure=configure)
    )

    # Build the jobs plugin
    jobs_plugin = (
        CliJobsBuilder()
        .add_job(SampleHealthCheckJob(), CliJobOptions(
            name="health-check",
            description="Periodic health check",
            interval="30s",
        ))
        .build(broadcast_fn=lambda msg: result.event_socket_manager.broadcast_message(msg))
    )

    result.mount_plugin(jobs_plugin)

    # Build the admin plugin
    admin_plugin = (
        CliAdminBuilder()
        .build(AdminBuildDeps(
            registry=result.registry,
            event_socket_manager=result.event_socket_manager,
            builder=result.builder,
            broadcast_fn=lambda msg: result.event_socket_manager.broadcast_message(msg),
            enabled_features=["jobs"],
        ))
    )

    result.mount_plugin(admin_plugin)

    # Register the module registry as a processor filter so that toggling
    # a plugin off in the admin dashboard actually blocks command execution.
    if admin_plugin.module_registry and result.executor:
        result.executor.add_filter(admin_plugin.module_registry)

    # Wire scheduler lifecycle into the app lifespan
    original_lifespan = result.app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_jobs(app: object) -> AsyncIterator[None]:
        await jobs_plugin.scheduler.start()
        async with original_lifespan(app) as state:  # type: ignore[arg-type]
            yield state
        await jobs_plugin.scheduler.stop()

    result.app.router.lifespan_context = lifespan_with_jobs

    logger.info("Qodalis CLI Demo Server (Python) running on http://%s:%d", host, port)
    logger.info("  API: http://%s:%d/api/qcli", host, port)
    logger.info("  Admin API: http://%s:%d/api/v1/qcli/admin", host, port)
    if admin_plugin.dashboard_app:
        logger.info("  Admin Dashboard: http://%s:%d/qcli/admin", host, port)
    logger.info("  WebSocket: ws://%s:%d/ws/qcli/events", host, port)
    logger.info("  File storage: %s", type(file_storage_provider).__name__)

    uvicorn.run(result.app, host=host, port=port)


if __name__ == "__main__":
    main()
