"""Demo CLI server with sample processors.

This demo showcases core CLI commands, the weather plugin, the jobs plugin,
the admin dashboard plugin, and the pluggable file-storage provider system.
By default the server uses an in-memory file store, but you can switch to any
of the available providers by uncommenting the relevant section below.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn

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
from qodalis_cli_data_explorer_sql import SqlDataExplorerProvider
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

    result = create_cli_server(
        CliServerOptions(
            configure=lambda builder: (
                builder
                .add_processor(CliEchoCommandProcessor())
                .add_processor(CliStatusCommandProcessor())
                .add_processor(CliTimeCommandProcessor())
                .add_processor(CliHelloCommandProcessor())
                .add_processor(CliMathCommandProcessor())
                .add_processor(CliSystemCommandProcessor())
                .add_processor(CliHttpCommandProcessor())
                .add_processor(CliHashCommandProcessor())
                .add_processor(CliBase64CommandProcessor())
                .add_module(WeatherModule())
                .add_data_explorer_provider(
                    SqlDataExplorerProvider(":memory:"),
                    DataExplorerProviderOptions(
                        name="sql",
                        description="In-memory SQLite database for ad-hoc queries",
                    ),
                )
                .set_file_storage_provider(file_storage_provider)
                .add_filesystem(FileSystemOptions(allowed_paths=["/tmp", "/app", "/home"]))
            ),
        )
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

    result.app.include_router(jobs_plugin.router, prefix="/api/v1/qcli/jobs")

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

    result.app.include_router(admin_plugin.router, prefix="/api/v1/qcli")

    # Mount the admin dashboard SPA (if the dist directory was found)
    if admin_plugin.dashboard_app:
        result.app.mount("/qcli/admin", admin_plugin.dashboard_app)

    # Wire scheduler lifecycle into the app lifespan
    original_lifespan = result.app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_jobs(app: object) -> AsyncIterator[None]:
        await jobs_plugin.scheduler.start()
        async with original_lifespan(app) as state:  # type: ignore[arg-type]
            yield state
        await jobs_plugin.scheduler.stop()

    result.app.router.lifespan_context = lifespan_with_jobs

    print(f"Qodalis CLI Demo Server (Python) running on http://{host}:{port}")
    print(f"  API: http://{host}:{port}/api/qcli")
    print(f"  Admin API: http://{host}:{port}/api/v1/qcli/admin")
    if admin_plugin.dashboard_app:
        print(f"  Admin Dashboard: http://{host}:{port}/qcli/admin")
    print(f"  WebSocket: ws://{host}:{port}/ws/qcli/events")
    print(f"  File storage: {type(file_storage_provider).__name__}")

    uvicorn.run(result.app, host=host, port=port)


if __name__ == "__main__":
    main()
