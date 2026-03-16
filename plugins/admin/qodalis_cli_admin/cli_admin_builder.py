"""Fluent builder for configuring and creating the admin plugin."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from fastapi import APIRouter
from starlette.responses import FileResponse

from .auth.jwt_service import JwtService
from .auth.auth_middleware import create_auth_dependency
from .auth.auth_controller import create_auth_router
from .controllers.status_controller import create_status_router
from .controllers.plugins_controller import create_plugins_router
from .controllers.config_controller import create_config_router
from .controllers.logs_controller import create_logs_router
from .controllers.ws_clients_controller import create_ws_clients_router
from .services.admin_config import AdminConfig
from .services.log_ring_buffer import LogRingBuffer
from .services.module_registry import ModuleRegistry
from .dashboard_resolver import resolve_dashboard_dir


BroadcastFn = Callable[[str], Awaitable[None]]


@dataclass
class AdminBuildDeps:
    """Dependencies required to build the admin plugin."""

    registry: Any  # CliCommandRegistry
    event_socket_manager: Any  # CliEventSocketManager
    builder: Any  # CliBuilder
    broadcast_fn: BroadcastFn | None = None


@dataclass
class CliAdminPlugin:
    """Result of building the admin plugin."""

    router: APIRouter
    auth_dependency: Any
    log_buffer: LogRingBuffer
    dashboard_app: Any  # ASGI app (SPA fallback) or None


class CliAdminBuilder:
    """Fluent builder for the admin dashboard plugin."""

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._jwt_secret: str = ""
        self._dashboard_path: str | None = None

    def set_credentials(self, username: str, password: str) -> CliAdminBuilder:
        """Set admin login credentials (overrides env vars)."""
        self._username = username
        self._password = password
        return self

    def set_jwt_secret(self, secret: str) -> CliAdminBuilder:
        """Set the JWT signing secret (overrides env var)."""
        self._jwt_secret = secret
        return self

    def set_dashboard_path(self, path: str) -> CliAdminBuilder:
        """Set an explicit path to the dashboard dist directory."""
        self._dashboard_path = path
        return self

    def build(self, deps: AdminBuildDeps) -> CliAdminPlugin:
        """Build the plugin, returning the router, auth dependency, and log buffer."""
        start_time = time.time()

        # Services
        config = AdminConfig(
            username=self._username,
            password=self._password,
            jwt_secret=self._jwt_secret,
        )
        jwt_service = JwtService(secret=config.jwt_secret)
        log_buffer = LogRingBuffer(broadcast_fn=deps.broadcast_fn)
        log_buffer.install_handler()
        module_registry = ModuleRegistry(deps.builder)
        auth_dep = create_auth_dependency(jwt_service)

        # Build the top-level router
        router = APIRouter()

        # Auth routes (login is public, /me is protected)
        auth_router = create_auth_router(config, jwt_service, auth_dependency=auth_dep)
        router.include_router(auth_router, prefix="/auth")

        # Protected routes
        status_router = create_status_router(start_time, deps.event_socket_manager, auth_dep)
        router.include_router(status_router)

        plugins_router = create_plugins_router(module_registry, auth_dep)
        router.include_router(plugins_router)

        config_router = create_config_router(config, auth_dep)
        router.include_router(config_router)

        logs_router = create_logs_router(log_buffer, auth_dep)
        router.include_router(logs_router)

        ws_clients_router = create_ws_clients_router(deps.event_socket_manager, auth_dep)
        router.include_router(ws_clients_router)

        # Resolve dashboard SPA
        dashboard_dir = resolve_dashboard_dir(self._dashboard_path)
        dashboard_app = None

        if dashboard_dir:
            async def spa_fallback(scope: dict, receive: Any, send: Any) -> None:
                """ASGI app that serves static files with index.html fallback for SPA routing."""
                if scope["type"] == "http":
                    path = scope.get("path", "/").lstrip("/")
                    file_path = os.path.join(dashboard_dir, path)

                    if path and os.path.isfile(file_path):
                        response = FileResponse(file_path)
                        await response(scope, receive, send)
                    else:
                        response = FileResponse(os.path.join(dashboard_dir, "index.html"))
                        await response(scope, receive, send)

            dashboard_app = spa_fallback

        return CliAdminPlugin(
            router=router,
            auth_dependency=auth_dep,
            log_buffer=log_buffer,
            dashboard_app=dashboard_app,
        )
