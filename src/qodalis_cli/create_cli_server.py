from __future__ import annotations

import logging
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from qodalis_cli_filesystem.providers.os_provider import OsFileStorageProvider, OsProviderOptions

from .controllers import create_cli_router, create_cli_router_v2, create_cli_version_router
from .controllers.filesystem_controller import create_filesystem_router
from .extensions import CliBuilder
from .filesystem import FileSystemPathValidator
from .services import (
    CliCommandExecutorService,
    CliCommandRegistry,
    CliEventSocketManager,
    CliLogSocketManager,
    CliShellSessionManager,
    WebSocketLogHandler,
)


@dataclass
class CliServerOptions:
    """Configuration options for creating a CLI server instance."""

    base_path: str = "/api/qcli"
    cors: bool = True
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    configure: Callable[[CliBuilder], None] | None = None


@dataclass
class CliServerResult:
    """Return value from ``create_cli_server`` containing the app and services."""

    app: FastAPI
    registry: CliCommandRegistry
    builder: CliBuilder
    event_socket_manager: CliEventSocketManager
    log_socket_manager: CliLogSocketManager


def create_cli_server(options: CliServerOptions | None = None) -> CliServerResult:
    """Create and configure a fully wired FastAPI CLI server.

    Args:
        options: Optional server configuration. Uses defaults if ``None``.

    Returns:
        A ``CliServerResult`` containing the FastAPI app and all services.
    """
    opts = options or CliServerOptions()

    event_socket_manager = CliEventSocketManager()
    log_socket_manager = CliLogSocketManager()
    shell_session_manager = CliShellSessionManager()

    log_handler = WebSocketLogHandler(log_socket_manager)
    logging.getLogger().addHandler(log_handler)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await event_socket_manager.broadcast_disconnect()
        await log_socket_manager.broadcast_disconnect()
        logging.getLogger().removeHandler(log_handler)

    app = FastAPI(title="Qodalis CLI Server", lifespan=lifespan)

    if opts.cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=opts.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    registry = CliCommandRegistry()
    builder = CliBuilder(registry)

    if opts.configure:
        opts.configure(builder)

    executor = CliCommandExecutorService(registry)
    router = create_cli_router(registry, executor)
    router_v2 = create_cli_router_v2(registry, executor)
    version_router = create_cli_version_router()

    app.include_router(router, prefix="/api/v1/qcli")
    app.include_router(router_v2, prefix="/api/v2/qcli")
    app.include_router(version_router, prefix="/api/qcli")

    if opts.base_path != "/api/v1/qcli":
        app.include_router(router, prefix=opts.base_path)

    if builder.file_storage_provider is not None:
        fs_router = create_filesystem_router(builder.file_storage_provider)
        app.include_router(fs_router, prefix="/api/qcli/fs")
    elif builder.filesystem_options is not None:
        os_provider = OsFileStorageProvider(
            OsProviderOptions(
                allowed_paths=builder.filesystem_options.allowed_paths
            )
        )
        fs_router = create_filesystem_router(os_provider)
        app.include_router(fs_router, prefix="/api/qcli/fs")

    if builder.data_explorer_registrations:
        from qodalis_cli_data_explorer import (
            DataExplorerRegistry,
            DataExplorerExecutor,
            create_data_explorer_router,
        )

        data_explorer_registry = DataExplorerRegistry()
        for reg in builder.data_explorer_registrations:
            data_explorer_registry.register(reg.provider, reg.options)
        data_explorer_executor = DataExplorerExecutor(data_explorer_registry)
        data_explorer_router = create_data_explorer_router(
            data_explorer_registry, data_explorer_executor
        )
        app.include_router(data_explorer_router, prefix="/api/qcli/data-explorer")

    @app.websocket("/ws/v1/qcli/events")
    async def websocket_events_v1(websocket: WebSocket) -> None:
        logger.debug("WebSocket connection accepted: events")
        await event_socket_manager.handle_connection(websocket)

    @app.websocket("/ws/qcli/events")
    async def websocket_events(websocket: WebSocket) -> None:
        logger.debug("WebSocket connection accepted: events")
        await event_socket_manager.handle_connection(websocket)

    async def _handle_logs(websocket: WebSocket) -> None:
        level_filter = websocket.query_params.get("level") or None
        logger.debug("Log WebSocket connection accepted (level=%s)", level_filter or "all")
        await log_socket_manager.handle_connection(websocket, level_filter)

    @app.websocket("/ws/v1/qcli/logs")
    async def websocket_logs_v1(websocket: WebSocket) -> None:
        await _handle_logs(websocket)

    @app.websocket("/ws/qcli/logs")
    async def websocket_logs(websocket: WebSocket) -> None:
        await _handle_logs(websocket)

    async def _handle_shell(websocket: WebSocket) -> None:
        await websocket.accept()
        cols = int(websocket.query_params.get("cols", "80")) or 80
        rows = int(websocket.query_params.get("rows", "24")) or 24
        cmd = websocket.query_params.get("cmd") or None
        logger.debug("Shell WebSocket connection accepted (cols=%d, rows=%d)", cols, rows)
        await shell_session_manager.handle_session(websocket, cols, rows, cmd)

    @app.websocket("/ws/v1/qcli/shell")
    async def websocket_shell_v1(websocket: WebSocket) -> None:
        await _handle_shell(websocket)

    @app.websocket("/ws/qcli/shell")
    async def websocket_shell(websocket: WebSocket) -> None:
        await _handle_shell(websocket)

    return CliServerResult(
        app=app,
        registry=registry,
        builder=builder,
        event_socket_manager=event_socket_manager,
        log_socket_manager=log_socket_manager,
    )
