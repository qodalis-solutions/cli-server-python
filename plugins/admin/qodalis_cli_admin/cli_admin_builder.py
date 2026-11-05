"""Fluent builder for configuring and creating the admin plugin."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Callable, Awaitable

from fastapi import APIRouter
from .spa_static_files import SPAStaticFiles

from .auth.jwt_service import JwtService
from .auth.auth_middleware import create_auth_dependency, create_users_auth_dependency
from .auth.auth_controller import create_auth_router
from .controllers.status_controller import create_status_router
from .controllers.plugins_controller import create_plugins_router
from .controllers.config_controller import create_config_router
from .controllers.logs_controller import create_logs_router
from .controllers.ws_clients_controller import create_ws_clients_router
from .services.admin_config import AdminConfig
from .services.admin_branding import validate_admin_branding
from .services.admin_theme import validate_admin_theme
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
    enabled_features: list[str] | None = None
    #: When set, the dashboard authenticates through the users plugin
    #: (``admin:access`` permission); static credentials are not used.
    users_plugin: Any = None
    #: The server's ``CliServerOptions`` (``result.options``): its
    #: ``external_authentication`` selects the dashboard's sign-in mode.
    #: Taken from the server by ``mount_plugin`` when not given.
    server_options: Any = None


@dataclass
class CliAdminPlugin:
    """Result of building the admin plugin."""

    prefix: str
    dashboard_prefix: str
    router: APIRouter
    auth_dependency: Any
    log_buffer: LogRingBuffer
    dashboard_app: Any  # ASGI app (SPA fallback) or None
    module_registry: ModuleRegistry | None = None
    #: Holds the server options read by ``GET /auth/mode`` (see on_mount).
    _server: dict = field(default_factory=dict, repr=False)

    def on_mount(self, server: Any) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: the sign-in mode
        follows the server's ``options.external_authentication``."""
        if self._server.get("options") is None:
            self._server["options"] = getattr(server, "options", None)


class CliAdminBuilder:
    """Fluent builder for the admin dashboard plugin."""

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._jwt_secret: str = ""
        self._dashboard_path: str | None = None
        self._trust_proxy = False
        self._users_plugin: Any = None
        self._theme: dict[str, Any] = validate_admin_theme(None)
        self._branding: dict[str, Any] | None = None
        self._sign_in_url: str | None = None

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

    def set_trust_proxy(self, trust: bool | int = True) -> CliAdminBuilder:
        """Trust ``X-Forwarded-For`` for login rate limiting (behind a proxy only).

        ``False``/``0`` uses the socket peer, ``True`` = 1 trusted hop, an int
        ``n`` = ``n`` hops: the client is the entry at ``len - n`` (the
        rightmost for one proxy), the leftmost if there are fewer entries.
        """
        self._trust_proxy = trust
        return self

    def set_theme(self, theme: Mapping[str, Any]) -> CliAdminBuilder:
        """Set the dashboard's color scheme.

        ``{"mode": "dark" | "light" | "system", "dark": {...}, "light": {...}}``
        with color overrides per scheme; tokens left out keep the dashboard
        defaults. Token keys are camelCase (``primaryHover``) or snake_case
        (``primary_hover``). Values must be plain CSS colors (hex,
        ``rgb()``/``hsl()``/``oklch()``... or a named color); anything else
        raises :class:`ValueError` here.

        Example::

            builder.set_theme({
                "mode": "system",
                "light": {"primary": "#2a6bba", "background": "#f1f6fa"},
                "dark": {"primary": "#4f9be0", "background": "#0d1823"},
            })
        """
        self._theme = validate_admin_theme(theme)
        return self

    def set_branding(self, branding: Mapping[str, Any]) -> CliAdminBuilder:
        """Ship the dashboard under your own name and icon, and name its terminal.

        ``{"name": "RateBell", "icon_url": "/favicon.svg", "cli_name": "RateBell CLI"}``:
        the name and icon of the browser tab, sidebar and sign-in page, and the
        terminal's name in its welcome banner. The icon is an absolute http(s)
        URL, a root-relative path or a base64 ``data:image`` URL. Unset values
        keep the Qodalis defaults (Qodalis Cli Admin, the Qodalis Solutions
        logo, Web CLI); a bad value raises :class:`ValueError` here.
        """
        self._branding = validate_admin_branding(branding)
        return self

    def _theme_json(self) -> bytes:
        """The ``theme.json`` body: the color scheme, plus ``branding`` when set."""
        doc = {**self._theme, "branding": self._branding} if self._branding else self._theme
        return json.dumps(doc, separators=(",", ":")).encode()

    def set_sign_in_url(self, url: str | None) -> CliAdminBuilder:
        """Where the host application signs users in, shown by the dashboard
        in external sign-in mode (users plugin with an external auth adapter)
        and returned by ``GET /api/v1/qcli/auth/mode``.  An absolute
        ``http(s)`` URL or a root-relative path (``/sign-in``); anything else
        raises :class:`ValueError` in :meth:`build`."""
        self._sign_in_url = url
        return self

    def set_users_plugin(self, users_plugin: Any) -> CliAdminBuilder:
        """Authenticate the dashboard through the users plugin."""
        self._users_plugin = users_plugin
        return self

    def build(self, deps: AdminBuildDeps) -> CliAdminPlugin:
        """Build the plugin, returning the router, auth dependency, and log buffer."""
        start_time = time.time()

        users_plugin = self._users_plugin or deps.users_plugin
        config = AdminConfig(
            username=self._username,
            password=self._password,
            jwt_secret=self._jwt_secret,
            warn_default=users_plugin is None,
            sign_in_url=self._sign_in_url,
        )
        config.validate()
        server: dict = {"options": deps.server_options}

        def is_external() -> bool:
            return bool(getattr(server["options"], "external_authentication", False))
        jwt_service = JwtService(secret=config.jwt_secret)
        log_buffer = LogRingBuffer(broadcast_fn=deps.broadcast_fn)
        log_buffer.install_handler()
        module_registry = ModuleRegistry(deps.builder)
        if users_plugin is not None:
            auth_dep = create_users_auth_dependency(users_plugin)
            allow = getattr(users_plugin, "allow_anonymous", None)
            if callable(allow):
                allow("/api/v1/qcli/auth/login", ["POST"])
        else:
            auth_dep = create_auth_dependency(jwt_service)

        router = APIRouter()

        auth_router = create_auth_router(
            config, jwt_service, auth_dependency=auth_dep,
            trust_proxy=self._trust_proxy, users_plugin=users_plugin,
            is_external=is_external,
        )
        router.include_router(auth_router, prefix="/auth")

        enabled_features: list[str] = list(deps.enabled_features or [])

        if "filesystem" not in enabled_features and (
            getattr(deps.builder, "file_storage_provider", None) is not None
            or getattr(deps.builder, "filesystem_options", None) is not None
        ):
            enabled_features.append("filesystem")

        if "jobs" not in enabled_features:
            for mod in getattr(deps.builder, "modules", []):
                mod_name = type(mod).__name__.lower()
                if "job" in mod_name:
                    enabled_features.append("jobs")
                    break

        status_router = create_status_router(
            start_time, deps.event_socket_manager, auth_dep, enabled_features,
            registry=deps.registry,
        )
        router.include_router(status_router)

        plugins_router = create_plugins_router(module_registry, auth_dep)
        router.include_router(plugins_router)

        config_router = create_config_router(config, auth_dep)
        router.include_router(config_router)

        logs_router = create_logs_router(log_buffer, auth_dep)
        router.include_router(logs_router)

        ws_clients_router = create_ws_clients_router(deps.event_socket_manager, auth_dep)
        router.include_router(ws_clients_router)

        dashboard_dir = resolve_dashboard_dir(self._dashboard_path)
        dashboard_app = None

        if dashboard_dir:
            dashboard_app = SPAStaticFiles(
                directory=dashboard_dir, html=True, theme_json=self._theme_json(),
            )

        return CliAdminPlugin(
            prefix="/api/v1/qcli",
            dashboard_prefix="/qcli/admin",
            router=router,
            auth_dependency=auth_dep,
            log_buffer=log_buffer,
            dashboard_app=dashboard_app,
            module_registry=module_registry,
            _server=server,
        )
