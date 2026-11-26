"""Fluent builder for configuring and creating the admin plugin."""

from __future__ import annotations

import json
import logging
import os
import time
import dataclasses
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Callable, Awaitable

from fastapi import APIRouter
from qodalis_cli_server_abstractions import CliRoutePermission
from qodalis_cli_server_abstractions.config import CLI_CONFIG_CHANGED_EVENT, CliConfigPermissions
from .spa_static_files import SPAStaticFiles

from .auth.jwt_service import JwtService
from .auth.auth_middleware import create_auth_dependency, create_users_auth_dependency
from .auth.auth_controller import create_auth_router
from .commands.admin_command import AdminCommandDeps, AdminModule, CliAdminCommandProcessor
from .controllers.status_controller import create_status_router, status_snapshot
from .controllers.plugins_controller import create_plugins_router
from .controllers.config_controller import create_config_router
from .controllers.logs_controller import create_logs_router
from .controllers.ws_clients_controller import create_ws_clients_router
from .controllers.dashboard_marketplace_controller import create_dashboard_marketplace_router
from .controllers.server_marketplace_controller import create_server_marketplace_router
from .controllers.updates_controller import create_updates_router
from .services.dashboard_marketplace import (
    DASHBOARD_INSTALL_PERMISSION,
    DashboardMarketplace,
    DashboardMarketplaceAssetRoute,
    DashboardMarketplaceDeps,
    DashboardMarketplaceOptions,
)
from .services.server_plugin_marketplace import (
    DEFAULT_PYPI_URL,
    PLUGINS_INSTALL_PERMISSION,
    SERVER_CORE_PACKAGE,
    ServerPluginMarketplace,
    ServerPluginMarketplaceDeps,
    ServerPluginMarketplaceOptions,
    default_wheel_fetch,
)
from .services.update_checker import UPDATES_FILE, UpdateChecker, UpdateCheckerDeps, UpdateCheckOptions
from .services.updates_mcp import UpdatesMcpToolProvider
from .services.admin_config import AdminConfig
from .services.plugins_folder import DATA_DIR_HINT, QCLI_DATA_DIR_ENV, unwritable_folders_warning
from .services.admin_config_providers import AdminAuthConfigProvider, create_logs_config_provider
from .services.admin_branding import validate_admin_branding
from .services.admin_theme import validate_admin_theme
from .services.log_ring_buffer import LogRingBuffer
from .services.module_registry import ModuleRegistry
from .services.status_history import StatusHistory
from .dashboard_resolver import resolve_dashboard_dir

logger = logging.getLogger("qcli-admin")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("qodalis-cli-server-plugin-admin")
    except Exception:  # pragma: no cover - not installed as a package
        return "1.0.0"


BroadcastFn = Callable[[str], Awaitable[None]]


@dataclass
class AdminBuildDeps:
    """Dependencies required to build the admin plugin."""

    registry: Any  # CliCommandRegistry
    event_socket_manager: Any  # CliEventSocketManager
    builder: Any  # CliBuilder
    #: Where the wire message of every log entry (``log:entry``) goes; by
    #: default they are published on ``event_bus`` with the audience ``logs:read``.
    broadcast_fn: BroadcastFn | None = None
    #: The server's event bus (``result.event_bus``); taken from the server
    #: by ``mount_plugin`` when neither it nor ``broadcast_fn`` is given.
    event_bus: Any = None
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
    #: Status samples of ``GET /status/history``; sampling runs with the
    #: app's lifespan once the plugin is mounted.
    status_history: StatusHistory | None = None
    #: ``config:read`` for ``GET /config``, ``config:write`` for ``PATCH``
    #: (enforced on a server protected by the users plugin).
    route_permissions: list[Any] = field(default_factory=list)
    #: The ``admin`` terminal command (``status``, ``plugins``, ``clients``,
    #: ``logs``), registered by ``mount_plugin``; needs ``admin:access`` on a
    #: protected server (``admin logs`` also ``logs:read``).
    module: Any = None
    #: The ``auth`` (read-only), ``logs`` and ``dashboard-plugins`` (with the
    #: marketplace) config providers, registered with the server's (also by
    #: ``build`` when the builder takes them).
    config_providers: list[Any] = field(default_factory=list)
    #: Dashboard plugins from the marketplace (``set_marketplace``), served at
    #: ``/api/v1/qcli/dashboard/marketplace``; ``None`` when turned off
    #: (``set_marketplace(False)``) or the builder cannot add pages at runtime.
    marketplace: DashboardMarketplace | None = None
    #: Server plugins from the marketplace (``/api/v1/qcli/server/marketplace``); ``None`` when the marketplace is
    #: off or the server has no runtime plugins (``builder.server_plugins``).
    server_marketplace: ServerPluginMarketplace | None = None
    #: Serves the marketplace plugins' files at ``/qcli/modules/<id>/`` (mounted by ``mount_plugin``).
    asset_routes: list[Any] = field(default_factory=list)
    #: The update checker (``GET /api/v1/qcli/updates``, ``POST /updates/check``): every 6 hours, a job of the jobs
    #: plugin when one is mounted; started with the app once the plugins are loaded.  ``None`` when the marketplace
    #: is off.
    updates: UpdateChecker | None = None
    #: The ``updates_list`` MCP tool (registered by ``mount_plugin``).
    mcp_providers: list[Any] = field(default_factory=list)
    #: Holds the server options read by ``GET /auth/mode`` (see on_mount).
    _server: dict = field(default_factory=dict, repr=False)

    def on_mount(self, server: Any) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: the sign-in mode
        follows the server's ``options.external_authentication`` and the log
        entries go to the server's event bus."""
        if self._server.get("options") is None:
            self._server["options"] = getattr(server, "options", None)
        # ``dashboard:extensions-changed`` goes to the server's bus unless the plugin was built with one
        if self._server.get("event_bus") is None:
            self._server["event_bus"] = getattr(server, "event_bus", None)
        # Log entries are published on the server's bus (``log:entry``, audience ``logs:read``)
        # unless the plugin was built with a bus or a broadcast function
        if self.log_buffer.event_bus is None and self.log_buffer.broadcast_fn is None:
            self.log_buffer.event_bus = getattr(server, "event_bus", None)
        # A module disabled from the dashboard or ``admin plugins disable`` is refused by the executor
        executor = getattr(server, "executor", None)
        if self.module_registry is not None and executor is not None and hasattr(executor, "add_filter"):
            executor.add_filter(self.module_registry)
        app = getattr(server, "app", None)
        router = getattr(app, "router", None)
        if self.status_history is not None and router is not None and not self._server.get("sampling"):
            # Sample while the app runs; cancelled on shutdown.
            router.lifespan_context = self.status_history.wrap_lifespan(router.lifespan_context)
            self._server["sampling"] = True
        if self.updates is not None and router is not None and not self._server.get("updates"):
            # The periodic update check, once the app started (the plugins loaded, the jobs plugin mounted)
            router.lifespan_context = _with_update_checks(self.updates, router.lifespan_context)
            self._server["updates"] = True


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
        self._config_store: Any = None
        self._marketplace: DashboardMarketplaceOptions | None = DashboardMarketplaceOptions()
        self._updates = UpdateCheckOptions()

    def set_update_checks(self, options: UpdateCheckOptions | Mapping[str, Any] | bool) -> CliAdminBuilder:
        """The periodic check for updates (marketplace plugins, plugins added in code, the server package):
        ``enabled`` (default true), ``interval`` (seconds, default 6 hours), ``initial_delay`` (seconds after a start,
        default 60).  ``False`` turns the periodic check off; "Check now" (``POST /api/v1/qcli/updates/check``) still
        works.  An :class:`UpdateCheckOptions` or a dict of its fields."""
        if options is False:
            self._updates = UpdateCheckOptions(enabled=False)
        elif options is True:
            self._updates = UpdateCheckOptions()
        elif isinstance(options, UpdateCheckOptions):
            self._updates = options
        else:
            self._updates = UpdateCheckOptions(**dict(options))
        return self

    def set_marketplace(self, options: DashboardMarketplaceOptions | Mapping[str, Any] | bool) -> CliAdminBuilder:
        """Dashboard plugins from the marketplace: ``url`` (default ``https://plugins.qodalis.com``), ``install``
        (``verified``, the default: listed versions of verified publishers only; ``listed``: any listed version;
        ``off``: browse only), ``dir`` (default ``<data_dir>/dashboard-plugins``: the server's ``data_dir``, else
        ``QCLI_DATA_DIR``, else ``./qcli-data``),
        and ``fetch`` / ``cdns`` / ``timeout`` for the HTTP client.  A :class:`DashboardMarketplaceOptions` or a
        dict of its fields; ``False`` turns the marketplace off (no routes, installed plugins not loaded),
        ``True`` restores the defaults.  An invalid url or policy raises :class:`ValueError` in :meth:`build`."""
        if options is False:
            self._marketplace = None
        elif options is True:
            self._marketplace = DashboardMarketplaceOptions()
        elif isinstance(options, DashboardMarketplaceOptions):
            self._marketplace = options
        else:
            self._marketplace = DashboardMarketplaceOptions(**dict(options))
        return self

    def set_config_store(self, store: Any) -> CliAdminBuilder:
        """Keeps the ``logs`` config provider's (log buffer size and level) changes in ``store`` (its own
        store) instead of the server's default one; the host's store for the
        provider's id (``config stores={...}``,
        ``builder.set_config_store(store, id)``) still wins."""
        self._config_store = store
        return self

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
        raises :class:`ValueError` in :meth:`build`.  The dashboard fills
        ``{returnTo}`` in it, or appends ``returnTo=``, with the dashboard page
        to come back to; the host opens that URL with ``#token=`` after
        signing the user in."""
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
        log_buffer = LogRingBuffer(broadcast_fn=deps.broadcast_fn, event_bus=deps.event_bus)
        log_buffer.install_handler()
        module_registry = ModuleRegistry(deps.builder)

        def anonymous_admin_allowed() -> bool:
            """The server's ``allow_anonymous_admin`` (or a protected server); false for a builder without it."""
            check = getattr(deps.builder, "anonymous_admin_allowed", None)
            return bool(check()) if callable(check) else False
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

        status_history = StatusHistory(deps.event_socket_manager)
        status_router = create_status_router(
            start_time, deps.event_socket_manager, auth_dep, enabled_features,
            registry=deps.registry, builder=deps.builder,
            status_history=status_history,
        )
        router.include_router(status_router)

        # Dashboard plugins from the marketplace: pages added and removed at runtime, served from this server
        server["event_bus"] = deps.event_bus
        updates_ref: list[UpdateChecker] = []

        def refresh_updates() -> None:
            """After an install, update or removal: the updates that no longer apply are dropped."""
            for checker in updates_ref:
                try:
                    checker.refresh_installed()
                except Exception as exc:  # noqa: BLE001
                    logger.error("refreshing the updates failed: %s", exc)
        marketplace: DashboardMarketplace | None = None
        # Kept in the server's data directory unless the marketplace names its own folder
        server_plugins = getattr(deps.builder, "server_plugins", None)
        data_dir = getattr(server_plugins, "data_dir", None) or os.path.abspath(
            os.environ.get(QCLI_DATA_DIR_ENV) or "qcli-data"
        )
        explicit_dashboard_dir = self._marketplace.dir if self._marketplace is not None else None
        dashboard_folder_hint = "Set the marketplace's dir to a writable folder" if explicit_dashboard_dir else DATA_DIR_HINT
        add_extension = getattr(deps.builder, "add_dashboard_extension", None)
        remove_extension = getattr(deps.builder, "remove_dashboard_extension", None)
        if self._marketplace is not None and callable(add_extension) and callable(remove_extension):

            async def publish(event_type: str, data: dict[str, Any]) -> None:
                bus = server.get("event_bus")
                if bus is not None:
                    from qodalis_cli_server_abstractions import CliEventAudience

                    await bus.publish(event_type, data, CliEventAudience(permission="admin:access"))
                refresh_updates()

            async def publish_config_changed(data: dict[str, Any]) -> None:
                bus = server.get("event_bus")
                if bus is not None:
                    from qodalis_cli_server_abstractions import CliEventAudience

                    await bus.publish(CLI_CONFIG_CHANGED_EVENT, data, CliEventAudience(permission=CliConfigPermissions.READ))

            async def config_ready() -> None:
                service = getattr(deps.builder, "config", None)
                ready = getattr(service, "ready", None)
                if callable(ready):
                    await ready()

            marketplace_options = dataclasses.replace(
                self._marketplace, dir=explicit_dashboard_dir or os.path.join(data_dir, "dashboard-plugins"),
            )
            marketplace = DashboardMarketplace(marketplace_options, DashboardMarketplaceDeps(
                folder_hint=dashboard_folder_hint,
                extension_ids=lambda: [e.id for e in getattr(deps.builder, "dashboard_extensions", None) or []],
                extensions=lambda: [
                    e.to_dict() if hasattr(e, "to_dict") else dict(e)
                    for e in getattr(deps.builder, "dashboard_extensions", None) or []
                ],
                add_extension=add_extension,
                remove_extension=remove_extension,
                server_plugins=lambda: [p.id for p in getattr(deps.builder, "installed_plugins", None) or []],
                audit=lambda: getattr(deps.builder, "audit_sink", None),
                publish=publish,
                publish_config_changed=publish_config_changed,
                config_ready=config_ready,
                anonymous_allowed=anonymous_admin_allowed,
            ))
            marketplace.load()
            router.include_router(
                create_dashboard_marketplace_router(marketplace, auth_dep), prefix="/dashboard/marketplace",
            )

        # Server plugins from the marketplace: Python packages installed into <data dir>/server-plugins, loaded at the
        # next start (the same marketplace options)
        server_marketplace: ServerPluginMarketplace | None = None
        server_folder_hint = DATA_DIR_HINT
        if self._marketplace is not None and server_plugins is not None and hasattr(server_plugins, "records"):
            plugins_dir = os.path.abspath(server_plugins.dir)
            if not plugins_dir.startswith(os.path.abspath(server_plugins.data_dir) + os.sep):
                server_folder_hint = "Set server_plugins.dir to a writable folder"

            async def publish_server(event_type: str, data: dict[str, Any]) -> None:
                bus = server.get("event_bus")
                if bus is not None:
                    from qodalis_cli_server_abstractions import CliEventAudience

                    await bus.publish(event_type, data, CliEventAudience(permission="admin:access"))
                refresh_updates()

            m = self._marketplace
            server_marketplace = ServerPluginMarketplace(ServerPluginMarketplaceOptions(
                url=m.url, install=m.install, fetch=m.fetch, download=m.download, timeout=m.timeout,
                pypi_url=m.pypi_url, pip=m.pip, server_version=m.server_version,
                trusted_dependencies=m.trusted_dependencies,
            ), ServerPluginMarketplaceDeps(
                plugins=server_plugins,
                installed_plugins=lambda: list(getattr(deps.builder, "installed_plugins", None) or []),
                restart=getattr(deps.builder, "restart", None),
                audit=lambda: getattr(deps.builder, "audit_sink", None),
                publish=publish_server,
                anonymous_allowed=anonymous_admin_allowed,
                folder_hint=server_folder_hint,
            ))
            router.include_router(
                create_server_marketplace_router(server_marketplace, auth_dep), prefix="/server/marketplace",
            )

        # Updates: the marketplace's newer versions and the server package's, checked every 6 hours and on demand
        updates: UpdateChecker | None = None
        if self._marketplace is not None and (marketplace is not None or server_marketplace is not None):
            m = self._marketplace

            def server_version() -> str | None:
                if server_marketplace is not None:
                    return server_marketplace.server_version
                if m.server_version:
                    return m.server_version
                core = next(
                    (p for p in getattr(deps.builder, "installed_plugins", None) or [] if p.id == SERVER_CORE_PACKAGE),
                    None,
                )
                return getattr(core, "version", None)

            def job_scheduler() -> Any:
                services = getattr(deps.builder, "plugin_services", None)
                return services.get("jobs.scheduler") if services is not None and services.has("jobs.scheduler") else None

            updates = UpdateChecker(UpdateCheckerDeps(
                server_version=server_version,
                fetch=m.download or m.fetch or default_wheel_fetch,
                dashboard=marketplace,
                server=server_marketplace,
                pypi_url=(m.pypi_url or DEFAULT_PYPI_URL).strip().rstrip("/"),
                timeout=m.timeout,
                notifications=lambda: getattr(deps.builder, "notifications", None),
                scheduler=job_scheduler,
                file=os.path.join(data_dir, UPDATES_FILE),
            ), self._updates)
            updates_ref.append(updates)
            router.include_router(create_updates_router(updates, auth_dep), prefix="/updates")

        # One WARNING at startup when installing would fail: a folder that cannot be created or written
        writable_folders: list[tuple[str, str]] = []
        if marketplace is not None and marketplace.install != "off":
            writable_folders.append((marketplace.dir, dashboard_folder_hint))
        if server_marketplace is not None and server_marketplace.install != "off" and getattr(server_plugins, "enabled", False):
            writable_folders.append((server_plugins.dir, server_folder_hint))
        folder_warning = unwritable_folders_warning(writable_folders)
        if folder_warning:
            logger.warning(folder_warning)

        plugins_router = create_plugins_router(module_registry, auth_dep, deps.builder, marketplace)
        router.include_router(plugins_router)

        # Config: the server's config providers (builder.config), with the plugin's own
        def auth_mode() -> str:
            if is_external():
                return "external"
            return "users" if users_plugin is not None else "password"

        config_providers: list[Any] = [
            AdminAuthConfigProvider(config, auth_mode),
            create_logs_config_provider(log_buffer).set_store(self._config_store),
        ]
        if marketplace is not None:
            # The dashboard plugins' settings: the host's store for ``dashboard-plugins``, else the default store
            config_providers.append(marketplace.config)
        add_config_provider = getattr(deps.builder, "add_config_provider", None)
        registry = getattr(deps.builder, "config_registry", None)
        if callable(add_config_provider):
            for provider in config_providers:
                if registry is None or not registry.has(provider.id):
                    add_config_provider(provider)
        config_router = create_config_router(lambda: getattr(deps.builder, "config", None), auth_dep)
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

        admin_command = CliAdminCommandProcessor(AdminCommandDeps(
            status=lambda: status_snapshot(
                start_time, deps.event_socket_manager, enabled_features,
                deps.registry, deps.builder, status_history,
            ),
            status_history=status_history,
            module_registry=module_registry,
            log_buffer=log_buffer,
            event_socket_manager=deps.event_socket_manager,
            installed_plugins=lambda: list(getattr(deps.builder, "installed_plugins", None) or []),
            marketplace=marketplace,
            restart=getattr(deps.builder, "restart", None),
            server_marketplace=server_marketplace,
        ))

        return CliAdminPlugin(
            module=AdminModule(admin_command, _package_version()),
            prefix="/api/v1/qcli",
            dashboard_prefix="/qcli/admin",
            router=router,
            auth_dependency=auth_dep,
            log_buffer=log_buffer,
            dashboard_app=dashboard_app,
            module_registry=module_registry,
            status_history=status_history,
            route_permissions=[
                CliRoutePermission(
                    path="/api/v1/qcli/config", read=CliConfigPermissions.READ, write=CliConfigPermissions.WRITE,
                ),
                CliRoutePermission(
                    path="/api/v1/qcli/dashboard/marketplace", read="admin:access", write=DASHBOARD_INSTALL_PERMISSION,
                ),
                CliRoutePermission(
                    path="/api/v1/qcli/server/marketplace", read="admin:access", write=PLUGINS_INSTALL_PERMISSION,
                ),
            ],
            config_providers=config_providers,
            marketplace=marketplace,
            server_marketplace=server_marketplace,
            asset_routes=[DashboardMarketplaceAssetRoute(marketplace)] if marketplace is not None else [],
            updates=updates,
            mcp_providers=[UpdatesMcpToolProvider(updates)] if updates is not None else [],
            _server=server,
        )


def _with_update_checks(checker: UpdateChecker, lifespan: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """The app's lifespan with the periodic update check running inside it (started once the server's own startup,
    which loads the plugins, is done)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def wrapped(app: Any) -> Any:
        async with lifespan(app) as state:
            checker.start()
            try:
                yield state
            finally:
                await checker.stop()

    return wrapped
