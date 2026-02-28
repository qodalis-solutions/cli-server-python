from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from .controllers import create_cli_router
from .extensions import CliBuilder
from .services import (
    CliCommandExecutorService,
    CliCommandRegistry,
    CliEventSocketManager,
)


@dataclass
class CliServerOptions:
    base_path: str = "/api/cli"
    cors: bool = True
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    configure: Callable[[CliBuilder], None] | None = None


@dataclass
class CliServerResult:
    app: FastAPI
    registry: CliCommandRegistry
    builder: CliBuilder
    event_socket_manager: CliEventSocketManager


def create_cli_server(options: CliServerOptions | None = None) -> CliServerResult:
    opts = options or CliServerOptions()

    event_socket_manager = CliEventSocketManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await event_socket_manager.broadcast_disconnect()

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
    app.include_router(router, prefix=opts.base_path)

    @app.websocket("/ws/cli/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await event_socket_manager.handle_connection(websocket)

    return CliServerResult(
        app=app,
        registry=registry,
        builder=builder,
        event_socket_manager=event_socket_manager,
    )
