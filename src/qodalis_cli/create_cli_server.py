from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, WebSocket
from starlette.routing import Match, Mount

from qodalis_cli_server_abstractions import CliRoutePermission
from qodalis_cli_server_abstractions.config import create_custom_config_provider
from qodalis_cli_filesystem.providers.os_provider import OsFileStorageProvider, OsProviderOptions

from .controllers import create_cli_router, create_cli_version_router
from .dashboard_modules import data_explorer_page, filesystem_page
from .controllers.filesystem_controller import create_filesystem_router
from .extensions import CliBuilder
from .filesystem import FileSystemPathValidator
from .filesystem.event_publishing_provider import EventPublishingFileStorageProvider
from .filesystem.filesystem_options import DEFAULT_MAX_UPLOAD_BYTES
from .mcp import (
    CliMcpOptions,
    CommandsMcpToolProvider,
    DataExplorerMcpToolProvider,
    FilesystemMcpToolProvider,
    McpGateMiddleware,
    McpToolRegistry,
    ServerInfoMcpToolProvider,
    create_mcp_router,
    resolve_mcp_options,
)
from .config import (
    CliConfigCommandProcessor,
    CliHostConfigProvider,
    CliHostEnvironmentOptions,
    CliHostOption,
    CliServerConfigProvider,
    FileSystemApiLimits,
    create_cors_config_provider,
    create_filesystem_config_provider,
    create_mcp_config_provider,
)
from .mcp import ConfigMcpToolProvider
from .mcp.endpoint import MCP_PATH
from .restart import (
    CliRestartRequestTracker,
    CliServerRestart,
    CliServerRestartOptions,
    CliServerRestartServices,
    create_restart_router,
)
from .security import LiveCorsMiddleware, WebSocketOriginMiddleware, get_auth_context
from .shell_security import CliShellOptions, ShellAccessProvider, remote_address
from .services import (
    CliCommandExecutorService,
    CliEventBus,
    CliCommandRegistry,
    CliEventSocketManager,
    CliLogSocketManager,
    CliShellSessionManager,
    DEFAULT_MAX_TABLE_ROWS,
    WebSocketLogHandler,
)


@dataclass
class CliServerConfigOptions:
    """The ``config`` option of :class:`CliServerOptions`: config providers."""

    #: The default store: where the writable settings of every provider
    #: without its own store are kept (default in memory); same as
    #: ``builder.set_config_store(store)``.
    store: Any = None
    #: A store per provider id, winning over the provider's own and the
    #: default store: ``{"jobs": redis_store, "users": database_store}``; same
    #: as ``builder.set_config_store(store, id)``.  A store may be given to
    #: several providers.
    stores: dict[str, Any] = field(default_factory=dict)
    #: Which environment variables the ``host`` provider lists (default every one), secrets masked.
    environment: CliHostEnvironmentOptions | None = None
    #: Register the ``config`` command (default true).
    command: bool = True


@dataclass
class CliServerOptions:
    """Configuration options for creating a CLI server instance."""

    base_path: str = "/api/qcli"
    #: ``True`` forces CORS on, ``False`` off; ``None`` (default) enables it
    #: only when ``allowed_origins`` or ``cors_origins`` is given.
    cors: bool | None = None
    #: Explicit CORS origins (defaults to ``allowed_origins``).
    cors_origins: list[str] | None = None
    #: Origins accepted for CORS and every WebSocket upgrade (``*`` globs
    #: allowed, e.g. ``http://127.0.0.1:*``).  ``None`` = no Origin check.
    #: Same-origin requests (the server's own origin) are always accepted.
    allowed_origins: list[str] | None = None
    #: Interactive PTY shell WebSocket (``/ws/qcli/shell``) — opt-in.  Once
    #: enabled, access needs the users plugin, ``shell.token``,
    #: ``shell.authorize`` or ``shell.allow_anonymous``; otherwise the shell
    #: is locked (every connection closed with 4401).
    enable_shell: bool = False
    #: Remote shell security and runtime options (access, limits, env).
    shell: CliShellOptions = field(default_factory=CliShellOptions)
    #: MCP endpoint (``POST /api/qcli/mcp``) — opt-in.  ``True`` or a
    #: :class:`CliMcpOptions` (or an equivalent ``dict``) enables it.
    mcp: bool | CliMcpOptions | dict[str, Any] | None = None
    #: The most rows a table may return without paging (0 = no limit).  A
    #: larger table from a command that does not page (``write_paged_table``)
    #: is cut to this many rows and marked ``truncated``, so a terminal never
    #: receives thousands of rows at once.
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS
    #: ``True`` when requests are authenticated by the host application rather
    #: than by the CLI's own sign-in.  Set by an authentication plugin (the
    #: users plugin with an external auth adapter) when mounted; the admin
    #: dashboard then signs in through the host (``GET /api/v1/qcli/auth/mode``).
    external_authentication: bool = False
    #: ``False`` serves none of the CLI's HTTP routes and sockets
    #: (``/api/qcli/*``, ``/api/v1/qcli/*``, ``/ws/*``, MCP, filesystem, data
    #: explorer), e.g. for a service that only guards a public API with the
    #: users plugin in guard-only mode.  :meth:`CliServerResult.mount_plugin`
    #: then mounts only plugins that serve host routes (``cli_routes =
    #: False``).  The permission catalog, events and services stay.
    cli_routes: bool = True
    #: ``False`` turns off FastAPI's own ``/docs``, ``/redoc`` and
    #: ``/openapi.json`` for the whole app.
    app_docs: bool = True
    #: The dashboard pages the core serves (``filesystem``, ``data-explorer``, see
    #: :mod:`qodalis_cli.dashboard_modules`): by id, another build's directory or ``False`` to leave the page out.
    #: By default each is served from the copy in its package when the feature is on.
    dashboard_pages: dict[str, str | bool] = field(default_factory=dict)
    #: Config providers: the stores of writable settings (default and per provider), what the ``host``
    #: provider shows, the ``config`` command (:class:`CliServerConfigOptions`).
    config: CliServerConfigOptions | None = None
    #: Restarting the server from the dashboard, ``admin restart`` or ``POST /api/v1/qcli/server/restart``
    #: (:class:`CliServerRestartOptions` or a dict of its fields): ``mode`` ``auto`` (default: ``exit`` under a
    #: detected supervisor, else not supported), ``exit`` (graceful shutdown, then exit with ``exit_code``, default
    #: 75, for Docker, systemd, PM2 or Kubernetes to start it again), ``self`` (graceful shutdown, then the same
    #: command runs again in this process: bare runs and development) or ``handler`` (the host's function).
    restart: CliServerRestartOptions | dict[str, Any] | None = None
    configure: Callable[[CliBuilder], None] | None = None


#: The core's own API and socket prefixes (see ``CliServerResult.cli_route_prefixes``).
CORE_CLI_ROUTE_PREFIXES: tuple[str, ...] = (
    "/api/qcli", "/api/v1/qcli", "/api/cli", "/api/v1/cli", "/ws/qcli", "/ws/v1/qcli",
)


@dataclass
class CliServerResult:
    """Return value from ``create_cli_server`` containing the app and services."""

    app: FastAPI
    registry: CliCommandRegistry
    builder: CliBuilder
    event_socket_manager: CliEventSocketManager
    log_socket_manager: CliLogSocketManager
    executor: CliCommandExecutorService | None = None
    options: CliServerOptions = field(default_factory=CliServerOptions)
    shell_session_manager: CliShellSessionManager | None = None
    #: MCP tool providers — ``result.mcp.add_provider(provider)`` (plugins
    #: register theirs when mounted).  Present even when MCP is disabled.
    mcp: McpToolRegistry = field(default_factory=McpToolRegistry)
    #: The server's event bus: ``await result.event_bus.publish("ns:name",
    #: {...}, CliEventAudience(permission=...))`` reaches the events
    #: WebSocket (``/ws/v1/qcli/events``) and in-process subscribers
    #: (``result.event_bus.subscribe("job:*", handler)``).  Plugins mounted
    #: with ``mount_plugin`` publish through it.
    event_bus: CliEventBus = field(default_factory=CliEventBus)
    #: Path prefixes of the CLI's own routes: the core's API and sockets, the
    #: custom ``base_path`` and the prefixes (API and dashboard) of every
    #: plugin mounted with :meth:`mount_plugin`.  Anything else the app
    #: serves is the host application's.  Kept up to date as plugins mount.
    cli_route_prefixes: list[str] = field(default_factory=lambda: list(CORE_CLI_ROUTE_PREFIXES))
    #: Host paths served without sign-in (GET and HEAD), also under ``/api/``
    #: on a server protected by the users plugin: the ``public_paths`` of
    #: mounted plugins (portal, docs, the public-api plugin's document).  An
    #: entry ending in ``/*`` covers everything under it.  Never a CLI route.
    public_paths: list[str] = field(default_factory=list)
    #: Permissions mounted plugins declared for their routes
    #: (``route_permissions``: :class:`CliRoutePermission`), enforced by the
    #: users plugin on a protected server: ``read`` for GET and HEAD,
    #: ``write`` otherwise; the longest path wins.
    route_permissions: list[Any] = field(default_factory=list)
    #: The live list of allowed origins (CORS, WebSocket upgrades, MCP, the
    #: users plugin's CSRF check): the ``cors`` config provider changes its
    #: items at runtime.
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def restart(self) -> CliServerRestart:
        """Restarting the server and the changes waiting for it (same as ``builder.restart``): ``mark_required``,
        ``clear_required``, ``status()``, ``on_shutdown(name, fn)`` for the host's own clean-up (pools, stores),
        ``attach_server(uvicorn_server)`` when uvicorn runs outside the main thread."""
        return self.builder.restart

    @property
    def config(self) -> Any:
        """The server's configuration (same as ``builder.config``): ``await
        result.config.ready()`` applies every provider's stored settings (the
        app's startup does it too)."""
        return self.builder.config

    def add_cli_route_prefix(self, prefix: str | None) -> None:
        """Count ``prefix`` among the CLI's routes (see
        :attr:`cli_route_prefixes`).  :meth:`mount_plugin` does it for every
        plugin it mounts; a plugin whose router the host includes by hand
        (``app.include_router``) is not counted unless the host calls this."""
        if not isinstance(prefix, str) or not prefix.strip("/"):
            return
        normalized = "/" + prefix.strip().strip("/")
        if normalized not in self.cli_route_prefixes:
            self.cli_route_prefixes.append(normalized)

    def add_dashboard_page(self, page: Any) -> None:
        """Serve a dashboard module page (:class:`DashboardModulePage`: files at its prefix, outside the API) and
        list its extension in the admin dashboard.  A second page with the same id is skipped with a warning;
        without CLI routes nothing is served."""
        if not self.options.cli_routes:
            return
        page_id = page.extension.get("id")
        if any(e.id == page_id for e in self.builder.dashboard_extensions):
            logger.warning("[qcli] the dashboard page %s is already served; %s is not mounted", page_id, page.prefix)
            return
        self.add_cli_route_prefix(page.prefix)
        self.builder.add_dashboard_extension(page.extension)
        self.app.mount(page.prefix, page.app)

    def set_shell_access_provider(self, provider: ShellAccessProvider | None) -> None:
        """Let an auth plugin own shell access (the users plugin calls this
        when mounted, selecting the ``users`` shell mode)."""
        if self.shell_session_manager is not None:
            self.shell_session_manager.set_access_provider(provider)

    def mount_plugin(self, plugin: object) -> None:
        """Mount a plugin using its built-in prefix.

        Reads ``prefix`` and ``router`` from the plugin result, and optionally
        ``dashboard_prefix`` / ``dashboard_app`` for plugins that serve a UI and
        ``dashboard_pages`` (dashboard module pages, see :meth:`add_dashboard_page`).
        A plugin with ``cli_routes = False`` serves host routes (public API,
        docs, portal): its prefix is not counted among the CLI's routes, and
        it is the only kind mounted on a server without CLI routes.

        Example::

            result = create_cli_server()
            result.mount_plugin(jobs_plugin)
            result.mount_plugin(admin_plugin)
        """
        # Listed among the installed plugins (admin dashboard), once, in the
        # order mounted; a plugin object of the host's own code is not listed.
        self.builder.add_installed_plugin(plugin)

        # A plugin may decline to be mounted again, e.g. the audit plugin when
        # the server already serves an audit log (``before_mount`` -> False).
        before_mount = getattr(plugin, "before_mount", None)
        if callable(before_mount) and before_mount(self) is False:
            return

        prefix = getattr(plugin, "prefix", None)
        router = getattr(plugin, "router", None)
        serves_cli_routes = getattr(plugin, "cli_routes", True) is not False
        if serves_cli_routes:
            self.add_cli_route_prefix(prefix)
            self.add_cli_route_prefix(getattr(plugin, "dashboard_prefix", None))
        public_paths = [] if serves_cli_routes else _checked_public_paths(plugin, prefix, self.cli_route_prefixes)
        routes_off = serves_cli_routes and not self.options.cli_routes
        if routes_off:
            if prefix and router is not None and getattr(router, "routes", None):
                logger.warning("[qcli] CLI routes are turned off; %s is not mounted", prefix)
        elif prefix and router:
            routes = self.app.router.routes
            count = len(routes)
            # A plugin mounted at the root (the portal at "/") has no prefix.
            self.app.include_router(router, prefix="" if prefix == "/" else prefix)
            if public_paths:
                # Paths served without sign-in are answered by the plugin
                # itself, never by a host route added earlier under them.
                routes[:] = routes[count:] + routes[:count]
        for path in public_paths:
            if path not in self.public_paths:
                self.public_paths.append(path)

        # Permissions of the plugin's routes, under its own prefix
        for rule in getattr(plugin, "route_permissions", None) or []:
            self.route_permissions.append(_checked_route_permission(rule, prefix))
        # The plugin's terminal commands, its permissions (catalog, ``cli``
        # surface) and its MCP tools
        module = getattr(plugin, "module", None)
        if module is not None:
            self.builder.add_module(module)
        permissions = getattr(plugin, "permissions", None)
        if permissions:
            known = {p.name for p in self.builder.permissions.all()}
            fresh = [p for p in permissions if _permission_name(p) not in known]
            if fresh:
                self.builder.add_permissions(fresh, surface="cli")
        for provider in getattr(plugin, "mcp_providers", None) or []:
            self.mcp.add_provider(provider)
        # The plugin's settings (a provider the host registered under the same id wins)
        for provider in getattr(plugin, "config_providers", None) or []:
            if not self.builder.config_registry.has(provider.id):
                self.builder.add_config_provider(provider)

        # Pages the plugin adds to the admin dashboard
        for extension in getattr(plugin, "dashboard_extensions", None) or []:
            self.builder.add_dashboard_extension(extension)
        if not routes_off:
            for page in getattr(plugin, "dashboard_pages", None) or []:
                self.add_dashboard_page(page)
            # Files the plugin serves outside the API (no sign-in); a route declines what it does not serve
            for asset in getattr(plugin, "asset_routes", None) or []:
                self.app.router.routes.append(_AssetRoute(asset))

        # Plugins may hook into the server (e.g. the users plugin protects
        # every route and scopes events to authenticated sockets).
        on_mount = getattr(plugin, "on_mount", None)
        if callable(on_mount):
            on_mount(self)

        # A restart's graceful shutdown stops the plugin's job scheduler and releases its resources
        scheduler = getattr(plugin, "scheduler", None)
        stop = getattr(scheduler, "stop", None)
        if callable(stop):
            self.restart.on_shutdown("job scheduler", stop)
        dispose = getattr(plugin, "dispose", None)
        if callable(dispose):
            self.restart.on_shutdown(f"plugin {prefix or type(plugin).__name__}", dispose)

        dashboard_prefix = getattr(plugin, "dashboard_prefix", None)
        dashboard_app = getattr(plugin, "dashboard_app", None)
        if dashboard_prefix and dashboard_app and routes_off:
            logger.warning(
                "[qcli] the CLI routes are turned off (cli_routes=False); the admin dashboard is not served",
            )
        elif dashboard_prefix and dashboard_app:
            self.app.mount(dashboard_prefix, dashboard_app)


class _AssetRoute(Mount):
    """A mounted plugin's asset route (``asset_routes``: ``prefix``, ``app`` and an optional ``handles(path)``):
    the app at the prefix, matching only the paths ``handles`` accepts (the path below the prefix), so other
    routes under the same prefix (``/qcli/modules/<id>`` of other plugins) still answer the rest."""

    def __init__(self, asset: Any) -> None:
        prefix = "/" + str(getattr(asset, "prefix")).strip("/")
        super().__init__(prefix, app=getattr(asset, "app"))
        self._handles = getattr(asset, "handles", None)

    def matches(self, scope: Any) -> Any:
        match, child = super().matches(scope)
        if match is Match.FULL and callable(self._handles):
            path = scope.get("path", "")
            below = path[len(self.path):] if path.startswith(self.path) else path
            if not self._handles(below or "/"):
                return Match.NONE, {}
        return match, child


def _permission_name(entry: Any) -> Any:
    return entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)


def _checked_route_permission(rule: Any, prefix: Any) -> Any:
    """A plugin's route permission: its path is its prefix or under it (on a
    segment boundary), else ``ValueError`` (``invalid route permission: <path>``)."""
    path = getattr(rule, "path", None)
    mount = "/" + prefix.strip("/") if isinstance(prefix, str) and prefix.strip("/") else None
    valid = (
        mount is not None
        and isinstance(path, str)
        and (path.rstrip("/") == mount or path.startswith(mount + "/"))
        and isinstance(getattr(rule, "read", None), str)
    )
    if not valid:
        raise ValueError(f"invalid route permission: {path}")
    return rule


_CLI_PATH = re.compile(r"^/(api|ws)/(v\d+/)?(qcli|cli)(/|$)")


def _checked_public_paths(plugin: object, prefix: object, cli_prefixes: list[str]) -> list[str]:
    """A host-route plugin's ``public_paths``: each one under the plugin's
    own mount (on a segment boundary), never ``/*`` alone and never a CLI
    route; anything else fails startup (``ValueError``)."""
    paths = list(getattr(plugin, "public_paths", None) or [])
    if not paths:
        return []
    mount = "/" + prefix.strip("/") if isinstance(prefix, str) and prefix.strip("/") else None
    for path in paths:
        base = path[:-2] if isinstance(path, str) and path.endswith("/*") else path
        valid = (
            mount is not None
            and isinstance(base, str)
            and (base == mount or base.startswith(mount + "/"))
            and "*" not in base
            and "//" not in base
            and not any(seg in (".", "..") for seg in base.split("/"))
            and not _CLI_PATH.match(base + "/")
            and not any(base == p or base.startswith(p + "/") or p.startswith(base + "/") for p in cli_prefixes)
        )
        if not valid:
            raise ValueError(f"invalid public path: {path}")
    return paths


def create_cli_server(options: CliServerOptions | None = None) -> CliServerResult:
    """Create and configure a fully wired FastAPI CLI server.

    Args:
        options: Optional server configuration. Uses defaults if ``None``.

    Returns:
        A ``CliServerResult`` containing the FastAPI app and all services.
    """
    opts = options or CliServerOptions()

    event_bus = CliEventBus()
    event_socket_manager = CliEventSocketManager()
    event_socket_manager.attach(event_bus)
    log_socket_manager = CliLogSocketManager()
    shell_session_manager = CliShellSessionManager(opts.shell, enabled=opts.enable_shell)

    log_handler = WebSocketLogHandler(log_socket_manager)
    logging.getLogger().addHandler(log_handler)

    #: Set to the result below (the lifespan applies the stored settings).
    server_ref: list[CliServerResult] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        shell_session_manager.log_startup()
        if server_ref:
            # Apply the stored settings before serving (a store that cannot be read fails startup)
            await server_ref[0].builder.config.ready()
        yield
        if server_ref:
            # A restart: the sockets closed with 1012, the shutdown hooks, then the mode's action
            await server_ref[0].builder.restart.server_stopped()
        await shell_session_manager.close_all()
        await event_socket_manager.broadcast_disconnect()
        await log_socket_manager.broadcast_disconnect()
        logging.getLogger().removeHandler(log_handler)

    if opts.app_docs:
        app = FastAPI(title="Qodalis CLI Server", lifespan=lifespan)
    else:
        app = FastAPI(
            title="Qodalis CLI Server", lifespan=lifespan,
            docs_url=None, redoc_url=None, openapi_url=None,
        )

    # The live list of allowed origins (CORS, sockets, MCP, CSRF): the ``cors`` config provider changes its items
    allowed_origins: list[str] = list(opts.allowed_origins or [])
    origins_configured = opts.allowed_origins is not None
    mcp_options = resolve_mcp_options(opts.mcp)
    if mcp_options is not None and opts.cli_routes:
        # Origin check and 405s before any auth middleware (users plugin).
        app.add_middleware(
            McpGateMiddleware,
            allowed_origins=lambda: allowed_origins,
            trust_proxy=lambda: opts.shell.trust_proxy,
        )

    cors_mode = _configure_cors(app, opts, allowed_origins)
    # Checked once origins are configured (also when added at runtime)
    app.add_middleware(
        WebSocketOriginMiddleware,
        allowed_origins=allowed_origins,
        trust_proxy=opts.shell.trust_proxy,
        enabled=lambda: origins_configured or len(allowed_origins) > 0,
    )

    registry = CliCommandRegistry()
    builder = CliBuilder(registry)
    if opts.config is not None and opts.config.store is not None:
        builder.set_config_store(opts.config.store)
    for provider_id, store in ((opts.config.stores or {}) if opts.config is not None else {}).items():
        builder.set_config_store(store, provider_id)

    if opts.configure:
        opts.configure(builder)

    async def close_sockets(code: int, reason: str) -> None:
        await shell_session_manager.close_all(reason, code)
        await event_socket_manager.broadcast_disconnect(code, reason)
        await log_socket_manager.broadcast_disconnect(code, reason)

    restart = builder.restart.configure(opts.restart, CliServerRestartServices(
        event_bus=lambda: event_bus,
        audit=lambda: builder.audit_sink,
        is_protected=lambda: _requires_authentication(server_ref[0]) if server_ref else False,
        close_sockets=close_sockets,
    ))
    # In-flight requests, which a restart's graceful shutdown waits for (outermost)
    app.add_middleware(CliRestartRequestTracker, restart=restart)

    executor = CliCommandExecutorService(registry, max_table_rows=opts.max_table_rows)
    executor.event_bus = event_bus
    shell_auth = lambda: shell_session_manager.access_mode  # noqa: E731
    router = create_cli_router(registry, executor, enable_shell=opts.enable_shell, shell_auth=shell_auth)
    version_router = create_cli_version_router(enable_shell=opts.enable_shell, shell_auth=shell_auth)

    if opts.cli_routes:
        app.include_router(router, prefix="/api/v1/qcli")
        app.include_router(version_router, prefix="/api/qcli")
        # Restart: GET the status (admin:access), POST restarts (server:restart)
        app.include_router(create_restart_router(restart), prefix="/api/v1/qcli/server/restart")

        if opts.base_path != "/api/v1/qcli":
            app.include_router(router, prefix=opts.base_path)

    mcp_tools = McpToolRegistry(mcp_options)
    package_version = _package_version()

    mcp_tools.add_provider(ServerInfoMcpToolProvider(
        package_version,
        registry,
        features=lambda: _mcp_features(mcp_tools, opts),
        shell_enabled=opts.enable_shell,
    ), builtin=True)

    fs_provider = None
    if builder.file_storage_provider is not None:
        fs_provider = builder.file_storage_provider
    elif builder.filesystem_options is not None:
        fs_provider = OsFileStorageProvider(
            OsProviderOptions(
                allowed_paths=builder.filesystem_options.allowed_paths
            )
        )
    fs_limits: FileSystemApiLimits | None = None
    fs_storage_name = (
        "os" if builder.file_storage_provider is None
        else str(getattr(builder.file_storage_provider, "name", None) or type(builder.file_storage_provider).__name__)
    )
    if fs_provider is not None:
        # The API and the MCP tools share it: one fs:changed per change.
        fs_provider = EventPublishingFileStorageProvider(fs_provider, event_bus)
        # Read per upload: the ``filesystem`` config provider changes it at runtime
        fs_limits = FileSystemApiLimits(
            builder.filesystem_options.max_upload_bytes
            if builder.filesystem_options is not None
            else DEFAULT_MAX_UPLOAD_BYTES
        )
        fs_router = create_filesystem_router(fs_provider, max_upload_bytes=fs_limits)
        if opts.cli_routes:
            app.include_router(fs_router, prefix="/api/qcli/fs")
        mcp_tools.add_provider(FilesystemMcpToolProvider(fs_provider), builtin=True)

    if builder.data_explorer_registrations:
        from qodalis_cli_data_explorer import (
            DataExplorerRegistry,
            DataExplorerExecutor,
            create_data_explorer_router,
        )

        data_explorer_registry = DataExplorerRegistry()
        for reg in builder.data_explorer_registrations:
            data_explorer_registry.register(reg.provider, reg.options)
        # The audit plugin can be mounted later (mount_plugin): read it per query
        data_explorer_executor = DataExplorerExecutor(
            data_explorer_registry,
            audit=lambda: _data_explorer_audit_sink(server_ref[0] if server_ref else None),
        )
        data_explorer_router = create_data_explorer_router(
            data_explorer_registry,
            data_explorer_executor,
            caller=lambda request: data_explorer_caller(
                get_auth_context(request), remote_address(request, opts.shell.trust_proxy),
            ),
        )
        if opts.cli_routes:
            app.include_router(data_explorer_router, prefix="/api/qcli/data-explorer")
        mcp_tools.add_provider(
            DataExplorerMcpToolProvider(
                data_explorer_registry,
                data_explorer_executor,
                caller=lambda context: data_explorer_caller(
                    context.auth,
                    remote_address(context.request, opts.shell.trust_proxy) if context.request is not None else None,
                ),
            ),
            builtin=True,
        )

    # The ``config`` command is served by the config_* tools instead
    mcp_tools.add_provider(CommandsMcpToolProvider(
        registry, executor, skip=lambda root: isinstance(root, CliConfigCommandProcessor),
    ))
    if mcp_options is not None and opts.cli_routes:
        app.include_router(create_mcp_router(
            mcp_tools,
            mcp_options,
            server_version=package_version,
            allowed_origins=lambda: allowed_origins,
            trust_proxy=lambda: opts.shell.trust_proxy,
        ))

    async def websocket_events(websocket: WebSocket) -> None:
        logger.debug("WebSocket connection accepted: events")
        await event_socket_manager.handle_connection(websocket, get_auth_context(websocket))

    async def websocket_logs(websocket: WebSocket) -> None:
        level_filter = websocket.query_params.get("level") or None
        logger.debug("Log WebSocket connection accepted (level=%s)", level_filter or "all")
        await log_socket_manager.handle_connection(websocket, level_filter, get_auth_context(websocket))

    async def websocket_shell(websocket: WebSocket) -> None:
        await shell_session_manager.handle_connection(websocket)

    if opts.cli_routes:
        # The shell is always routed: a disabled shell answers with close 4404 (C5).
        for name, handler in (
            ("events", websocket_events), ("logs", websocket_logs), ("shell", websocket_shell),
        ):
            app.add_api_websocket_route(f"/ws/v1/qcli/{name}", handler)
            app.add_api_websocket_route(f"/ws/qcli/{name}", handler)

    result = CliServerResult(
        app=app,
        registry=registry,
        builder=builder,
        event_socket_manager=event_socket_manager,
        log_socket_manager=log_socket_manager,
        executor=executor,
        options=opts,
        shell_session_manager=shell_session_manager,
        mcp=mcp_tools,
        event_bus=event_bus,
        allowed_origins=allowed_origins,
    )
    result.add_cli_route_prefix(opts.base_path)
    if opts.cli_routes:
        result.route_permissions.append(
            CliRoutePermission(path="/api/v1/qcli/server/restart", read="admin:access", write="server:restart"),
        )
    server_ref.append(result)
    _configure_config_providers(
        result, opts, package_version, cors_mode, mcp_options, fs_limits, fs_storage_name,
    )

    # The dashboard pages of the APIs the core serves
    if fs_provider is not None:
        page = filesystem_page(opts.dashboard_pages.get("filesystem"))
        if page is not None:
            result.add_dashboard_page(page)
    if builder.data_explorer_registrations:
        page = data_explorer_page(opts.dashboard_pages.get("data-explorer"))
        if page is not None:
            result.add_dashboard_page(page)
    return result


def data_explorer_caller(auth: Any, remote: str | None = None) -> Any:
    """The data explorer's caller for a server auth context (``None`` without
    an auth provider: the source's permission options are then not enforced)."""
    try:
        from qodalis_cli_server_abstractions import DataExplorerCaller
    except ImportError:  # pragma: no cover - older abstractions package
        from types import SimpleNamespace as DataExplorerCaller  # type: ignore[assignment]

    user = getattr(auth, "user", None)
    return DataExplorerCaller(
        user_id=getattr(auth, "user_id", None) or getattr(user, "id", None),
        user_name=getattr(auth, "user_name", None) or getattr(user, "name", None),
        remote_address=remote,
        has_permission=(lambda p: bool(auth.can(p))) if auth is not None else None,
    )


def _data_explorer_audit_sink(server: Any) -> Any:
    """Writes data explorer audit entries to the server's audit log (the
    audit plugin, mounted by the users plugin), or ``None`` without one."""
    plugin = getattr(server, "_qcli_audit_plugin", None) if server is not None else None
    if plugin is None:
        return None

    async def sink(entry: Any) -> None:
        from qodalis_cli_server_abstractions.audit import AuditEntry

        await plugin.log(AuditEntry(
            event=entry.event,
            username=entry.user_name,
            details=str(entry.metadata.get("source", "")),
            actor=entry.user_name,
            metadata=entry.metadata,
            user_id=entry.user_id or None,
            actor_id=entry.user_id or None,
        ))

    return sink


def _package_version() -> str:
    """Installed ``qodalis-cli-server`` version (MCP ``serverInfo``)."""
    try:
        from importlib.metadata import version

        return version("qodalis-cli-server")
    except Exception:  # pragma: no cover - not installed as a package
        from .controllers.cli_controller import SERVER_VERSION

        return SERVER_VERSION


def _mcp_features(tools: McpToolRegistry, opts: CliServerOptions) -> list[str]:
    """Enabled server features, as reported by the ``server_info`` tool."""
    features = [
        name for p in tools.providers
        if isinstance(name := getattr(p, "name", None), str) and name != "server"
    ]
    if opts.enable_shell:
        features.append("shell")
    if tools.enabled:
        features.append("mcp")
    return features


def _configure_cors(app: FastAPI, opts: CliServerOptions, allowed_origins: list[str]) -> str:
    """CORS is off unless origins are configured (or ``cors=True``); ``*``
    is never combined with credentials.  Returns the mode the ``cors`` config
    provider shows: ``disabled`` (``cors=False``), ``custom`` (``cors_origins``,
    fixed), ``permissive`` (``cors=True`` without origins: any origin) or
    ``origins`` (the live allowed origins, changeable at runtime)."""
    if opts.cors is False:
        return "disabled"
    if opts.cors_origins is not None:
        origins = list(opts.cors_origins) or ["*"]
        if opts.cors is None and not opts.cors_origins:
            return "disabled"
        app.add_middleware(LiveCorsMiddleware, origins=lambda: origins)
        return "custom"
    forced = opts.cors is True
    # While origins are allowed (the list changes at runtime); cors=True without any allows every origin
    app.add_middleware(
        LiveCorsMiddleware,
        origins=lambda: list(allowed_origins) if allowed_origins else (["*"] if forced else None),
    )
    return "permissive" if forced and not allowed_origins else "origins"


def _configure_config_providers(
    result: CliServerResult,
    opts: CliServerOptions,
    version: str,
    cors_mode: str,
    mcp_options: CliMcpOptions | None,
    fs_limits: FileSystemApiLimits | None,
    fs_storage_name: str,
) -> None:
    """The built-in config providers (a provider the host registered under the
    same id wins), the audit sink and event bus, the ``config`` command and
    the ``config_*`` MCP tools."""
    builder = result.builder
    config_options = opts.config or CliServerConfigOptions()

    def add_builtin(provider: Any) -> None:
        if not builder.config_registry.has(provider.id):
            builder.add_config_provider(provider)

    def host_options() -> list[CliHostOption]:
        shell = result.shell_session_manager
        return [
            CliHostOption("basePath", opts.base_path, "Base path", "Base path of the CLI routes"),
            CliHostOption("cliRoutes", opts.cli_routes, "CLI routes", "The CLI's routes and sockets are served"),
            CliHostOption("enableShell", opts.enable_shell, "Shell", "Interactive shell sessions are accepted"),
            CliHostOption(
                "shellAuth", str(getattr(shell, "access_mode", None) or "none"),
                "Shell access", "How shell sessions are authorized",
            ),
            CliHostOption(
                "trustProxy", int(opts.shell.trust_proxy),
                "Trusted proxy hops", "Reverse proxies trusted for the client address",
            ),
            CliHostOption(
                "maxTableRows", opts.max_table_rows, "Table rows", "Rows a table returns without paging (0: no limit)",
            ),
            CliHostOption(
                "externalAuthentication", opts.external_authentication,
                "External authentication", "The host application authenticates requests",
            ),
            CliHostOption(
                "protected", _requires_authentication(result),
                "Protected", "An auth provider protects every route",
            ),
        ]

    add_builtin(CliServerConfigProvider(version))
    add_builtin(CliHostConfigProvider(host_options, config_options.environment))
    add_builtin(create_cors_config_provider(result.allowed_origins, cors_mode))
    add_builtin(create_mcp_config_provider(mcp_options, MCP_PATH))
    if opts.cli_routes and fs_limits is not None:
        roots = builder.filesystem_options.allowed_paths if builder.filesystem_options is not None else None
        add_builtin(create_filesystem_config_provider(fs_storage_name, fs_limits, roots))
    add_builtin(create_custom_config_provider())
    # The server's audit log (the audit plugin, mounted by the users plugin), unless set_config_audit_store set one
    builder.attach_config_audit(lambda: _audit_log_sink(result))
    builder.config.attach(event_bus=lambda: result.event_bus)
    builder.config_registry.start()
    if opts.cli_routes and config_options.command is not False and result.registry.find_processor("config") is None:
        builder.add_processor(CliConfigCommandProcessor(builder.config))
    if mcp_options is not None:
        result.mcp.add_provider(ConfigMcpToolProvider(builder.config), builtin=True)


def _requires_authentication(result: CliServerResult) -> bool:
    check = getattr(result.event_socket_manager, "_requires_authentication", None)
    return bool(check()) if callable(check) else False


def _audit_log_sink(server: Any) -> Any:
    """Writes config changes to the server's audit log, or ``None`` without one."""
    plugin = getattr(server, "_qcli_audit_plugin", None)
    if plugin is None:
        return None
    return plugin.log
