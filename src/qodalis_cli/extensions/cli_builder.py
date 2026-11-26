from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    IDataExplorerProvider,
)

from ..abstractions import ICliCommandProcessor, ICliModule
from ..filesystem import FileSystemOptions
from ..models.cli_dashboard_extension import CliDashboardExtension
from ..models.cli_installed_plugin import (
    CORE_DISTRIBUTION,
    CliInstalledPlugin,
    distribution_of,
)
from ..permissions import (
    CORE_PERMISSIONS,
    CliPermission,
    CliPermissionCatalog,
    CliPermissionSurface,
)
from ..config.cli_config_registry import CliConfigRegistry
from ..config.cli_config_service import CliConfigService
from ..restart.cli_server_restart import CliServerRestart
from ..server_plugins.cli_server_plugins import CliServerPlugins, resolve_data_dir
from ..services.cli_command_registry import CliCommandRegistry

if TYPE_CHECKING:
    from qodalis_cli_filesystem import IFileStorageProvider


class _DataExplorerRegistration:
    """Internal holder for a provider + options pair."""

    __slots__ = ("provider", "options")

    def __init__(
        self,
        provider: IDataExplorerProvider,
        options: DataExplorerProviderOptions,
    ) -> None:
        self.provider = provider
        self.options = options


def _permission_name(entry: Any) -> Any:
    return entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", None)


#: The service the data explorer plugin's entry sets: rules every source added after it follows.
DATA_EXPLORER_POLICY_SERVICE = "data-explorer.policy"


def apply_data_explorer_policy(options: Any, policy: Any) -> Any:
    """A source's options under the data explorer's rules (``{force_read_only, max_rows, rate_limit}``)."""
    if not isinstance(policy, dict):
        return options
    from dataclasses import replace as _replace

    changes: dict[str, Any] = {}
    if policy.get("force_read_only"):
        changes["read_only"] = True
    max_rows = policy.get("max_rows")
    if isinstance(max_rows, int) and max_rows > 0:
        changes["max_rows"] = min(options.max_rows or max_rows, max_rows)
    if policy.get("rate_limit") is not None and getattr(options, "rate_limit", None) is None:
        changes["rate_limit"] = policy["rate_limit"]
    return _replace(options, **changes) if changes else options


class CliBuilder:
    """Fluent builder for registering command processors, modules, and services."""

    def __init__(self, registry: CliCommandRegistry) -> None:
        self._registry = registry
        self._modules: list[ICliModule] = []
        self._filesystem_options: FileSystemOptions | None = None
        self._file_storage_provider: IFileStorageProvider | None = None
        self._data_explorer_registrations: list[_DataExplorerRegistration] = []
        self._installed_plugins: list[CliInstalledPlugin] = []
        self._dashboard_extensions: list[CliDashboardExtension] = []
        self.add_installed_plugin(CORE_DISTRIBUTION)
        self._permissions = CliPermissionCatalog()
        self._permissions.add(CORE_PERMISSIONS, "cli")
        #: The config providers and the store of their writable settings (see :meth:`add_config_provider`).
        self.config_registry = CliConfigRegistry()
        self._config_audit_store: Any = None
        self._config_audit: Any = None
        #: The server's configuration (``GET/PATCH /api/v1/qcli/config``, the
        #: ``config`` command, the ``config_*`` MCP tools): providers filtered
        #: by permission, secrets masked, changes validated, audited
        #: (``config.update``) and published (``config:changed``).
        #: Restarting the server (``/api/v1/qcli/server/restart``, ``admin restart``) and the changes waiting for
        #: it: plugins mark theirs with ``restart.mark_required(id, source, description)``; the config service
        #: marks the settings with ``requires_restart``.
        self.restart = CliServerRestart()
        self._server_plugins: Any = CliServerPlugins(resolve_data_dir(), False)
        #: Whether callers who are not signed in may install, update and remove plugins and change settings (the
        #: ``config`` command, ``config_set``, ``admin plugins install``, ``admin dashboard install``): false by
        #: default; ``create_cli_server`` sets it to ``allow_anonymous_admin`` or a protected server.
        self.anonymous_admin_allowed: Callable[[], bool] = lambda: False
        self.config = CliConfigService(
            self.config_registry, audit=self._config_audit_sink, restart=lambda: self.restart,
            anonymous_writes=lambda: self.anonymous_admin_allowed(),
        )

    def add_permissions(
        self,
        entries: Iterable[CliPermission | Mapping[str, Any]],
        *,
        surface: CliPermissionSurface = "host",
    ) -> CliBuilder:
        """Declare permissions tokens can be given (``CliPermission`` or
        plain dicts with ``name``, ``description`` and ``group``).

        The host declares its own routes' permissions (``surface="host"``,
        the default), which ``host`` tokens may hold; plugins declare theirs
        with ``surface="cli"``.  Raises ``ValueError`` (``invalid permission
        entry: <name>`` / ``permission already registered: <name>``).
        """
        self._permissions.add(entries, surface)
        return self

    @property
    def permissions(self) -> CliPermissionCatalog:
        """The permission catalog: core's permissions, then plugins', then
        the host's."""
        return self._permissions

    def add_installed_plugin(
        self,
        plugin: str | object,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
    ) -> CliBuilder:
        """List a server plugin package among the installed plugins (once,
        in the order added), shown on the admin dashboard's plugins page.

        ``plugin`` is a distribution name (``qodalis-cli-server-plugin-users``)
        or an object (module, class or instance) of the package.  The built-in
        plugins list themselves when added or mounted; call it from a plugin of
        your own.  The host application's own code (no distribution) is never
        listed.  The icon defaults to the ``__plugin_icon__`` of the package's
        top-level ``__init__.py``.
        """
        if isinstance(plugin, CliInstalledPlugin):
            if not any(p.id == plugin.id for p in self._installed_plugins):
                self._installed_plugins.append(plugin)
            return self
        distribution = plugin if isinstance(plugin, str) else distribution_of(plugin)
        if not distribution or any(p.id == distribution for p in self._installed_plugins):
            return self
        self._installed_plugins.append(
            CliInstalledPlugin.from_distribution(
                distribution, name=name, description=description, icon=icon
            )
        )
        return self

    def remove_installed_plugin(self, plugin_id: str) -> bool:
        """Remove a package from the installed plugins (a runtime plugin that failed to load); ``False`` when it is
        not listed."""
        for i, p in enumerate(self._installed_plugins):
            if p.id == plugin_id:
                del self._installed_plugins[i]
                return True
        return False

    @property
    def server_plugins(self) -> Any:
        """The plugins installed at runtime from the marketplace (``<data dir>/server-plugins``): their installed list,
        where they are kept, and what this process loaded (``CliServerOptions(data_dir=..., server_plugins=...)``)."""
        return self._server_plugins

    def add_plugin(self, entry: Any, options: dict[str, Any] | None = None) -> CliBuilder:
        """Adds a server plugin in code through its entry (the module of its ``create_plugin(context)``, or the
        function): it loads at the app's startup in its declared order, its settings on the dashboard as when installed
        from there, ``options`` as their defaults (origin ``code``; saved settings override them).  Options that are
        not settings (a pool, a store, a function) reach the plugin as ``context.options``::

            from qodalis_cli_weather import plugin as weather
            builder.add_plugin(weather, {"defaultLocation": "Paris"})
        """
        self._server_plugins.add(entry, options)
        return self

    @property
    def plugin_services(self) -> Any:
        """What plugins share with each other (``context.services``): the users plugin, ..."""
        return self._server_plugins.services

    def update_installed_plugin(self, plugin_id: str, **changes: Any) -> bool:
        """Changes fields of a listed plugin (``status``, ``missing``, ``settings``); false when it is not listed."""
        from dataclasses import replace as _replace

        for i, p in enumerate(self._installed_plugins):
            if p.id == plugin_id:
                self._installed_plugins[i] = _replace(p, **changes)
                return True
        return False

    def use_server_plugins(self, server_plugins: Any) -> CliBuilder:
        """Set by ``create_cli_server`` from its ``data_dir`` and ``server_plugins`` options."""
        self._server_plugins = server_plugins
        return self

    def add_dashboard_extension(self, extension: CliDashboardExtension | Mapping[str, Any]) -> CliBuilder:
        """Add a page to the admin dashboard: the dashboard lists it in its
        navigation and loads ``entry`` (an ES module the plugin serves) when it
        is opened.  Served by the admin plugin at
        ``GET /api/v1/qcli/dashboard/extensions``, in registration order.
        Raises ``ValueError`` (``invalid dashboard extension: <reason>`` /
        ``dashboard extension already registered: <id>``).

        Example::

            builder.add_dashboard_extension(CliDashboardExtension(
                id="billing", title="Billing", icon="💳", group="data",
                permission="billing:read", entry="/billing/admin/extension.js",
                styles=("/billing/admin/extension.css",),
            ))
        """
        valid = CliDashboardExtension.of(extension).validated()
        if any(e.id == valid.id for e in self._dashboard_extensions):
            raise ValueError(f"dashboard extension already registered: {valid.id}")
        self._dashboard_extensions.append(valid)
        return self

    def remove_dashboard_extension(self, extension_id: str) -> bool:
        """Remove a dashboard extension at runtime (the admin plugin's
        marketplace plugins); ``GET /api/v1/qcli/dashboard/extensions`` reads
        the list on every request.  ``False`` when no extension has that id."""
        for i, e in enumerate(self._dashboard_extensions):
            if e.id == extension_id:
                del self._dashboard_extensions[i]
                return True
        return False

    @property
    def dashboard_extensions(self) -> list[CliDashboardExtension]:
        """The dashboard extensions registered so far, in registration order."""
        return list(self._dashboard_extensions)

    @property
    def installed_plugins(self) -> list[CliInstalledPlugin]:
        """The server plugin packages installed so far, in the order added."""
        return list(self._installed_plugins)

    def add_processor(self, processor: ICliCommandProcessor) -> CliBuilder:
        """Register a single command processor.

        Args:
            processor: The processor to register.

        Returns:
            This builder for chaining.
        """
        self._registry.register(processor)
        return self

    def add_module(self, module: ICliModule) -> CliBuilder:
        """Register a module and all of its processors.

        Args:
            module: The module whose processors will be registered.

        Returns:
            This builder for chaining.
        """
        self._modules.append(module)
        self.add_installed_plugin(module)
        for processor in module.processors:
            self._registry.register(processor, module.name)
        permissions = list(getattr(module, "permissions", None) or [])
        if permissions:
            # A name already in the catalog (the host's, or the module added twice) stays as registered
            known = {p.name.lower() for p in self._permissions.all()}
            fresh = [p for p in permissions if not isinstance(_permission_name(p), str) or _permission_name(p).lower() not in known]
            if fresh:
                self.add_permissions(fresh, surface="cli")
        for provider in getattr(module, "config_providers", None) or []:
            self.add_config_provider(provider)
        return self

    def add_config_provider(self, provider: Any) -> CliBuilder:
        """Register a config provider: its sections are listed by ``GET
        /api/v1/qcli/config`` and the ``config`` command; a provider that also
        implements ``ICliConfigWriter`` can be changed.  A
        ``CliStoredConfigProvider`` keeps its changes in a config store (see
        :meth:`set_config_store`).  Raises ``ValueError``: ``config provider already registered: <id>``.

        Example::

            builder.add_config_provider(CliStoredConfigProvider(
                id="billing", title="Billing",
                sections=[CliConfigSectionDefinition(id="invoices", settings=[
                    CliConfigSettingDefinition(key="dueDays", type="number", default=30,
                                               validation=CliConfigValidation(min=1)),
                ])],
            ))
        """
        self.config_registry.add(provider)
        return self

    @property
    def config_providers(self) -> list[Any]:
        """The registered config providers, sorted by ``order``, then id."""
        return self.config_registry.providers

    def set_config_store(self, store: Any, provider_id: str | None = None) -> CliBuilder:
        """Keep writable settings in ``store``: a ``JsonFileConfigStore`` (a
        file, or a section of the app's settings file), the databases
        plugin's ``CliDatabaseConfigStore``, the config-redis plugin's
        ``CliRedisConfigStore``, ...

        - Without ``provider_id``: the default store, for every provider
          without a store of its own (in memory, lost on restart, unless
          set).  Set it in ``configure``, before the server is created.
        - With ``provider_id``: the store of that provider only, winning over
          its own store.  Set it before the provider is registered (in
          ``configure`` for plugins mounted later); raises after.

        Example::

            builder.set_config_store(JsonFileConfigStore("config.json"))
            builder.set_config_store(CliRedisConfigStore(url=os.environ["REDIS_URL"]), "jobs")
        """
        self.config_registry.set_store(store, provider_id)
        return self

    def replace_config_store(self, store: Any) -> CliBuilder:
        """Replaces the default config store after the server was created (a config store plugin loaded at startup,
        e.g. config-redis): the providers using the default store move to ``store``."""
        self.config_registry.replace_default_store(store)
        return self

    def set_config_audit_store(self, store: Any) -> CliBuilder:
        """Write config changes (``config.update``) to this audit log
        (``IAuditStore``).  By default they go to the server's audit log (the
        audit plugin, mounted by the users plugin), when there is one."""
        self._config_audit_store = store
        return self

    def attach_config_audit(self, audit: Any) -> None:
        """Used by ``create_cli_server``: the default audit sink (a function returning one or ``None``)."""
        self._config_audit = audit

    @property
    def audit_sink(self) -> Any:
        """Where the server's own changes are audited (``config.update``, the
        admin plugin's ``dashboard.plugin.*``): the store set with
        :meth:`set_config_audit_store`, else the server's audit log; ``None``
        when there is neither.  An async function taking an ``AuditEntry``."""
        return self._config_audit_sink()

    def _config_audit_sink(self) -> Any:
        if self._config_audit_store is not None:
            return self._config_audit_store.log
        return self._config_audit() if self._config_audit is not None else None

    @property
    def modules(self) -> list[ICliModule]:
        """Return a copy of all registered modules."""
        return list(self._modules)

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Every registered command processor (modules' and those added on
        their own), as the command registry holds them."""
        return list(self._registry.processors)

    def add_filesystem(self, options: FileSystemOptions) -> CliBuilder:
        """Enable the filesystem API with the given options (legacy)."""
        self._filesystem_options = options
        self.add_installed_plugin("qodalis-cli-server-plugin-filesystem")
        return self

    def set_file_storage_provider(
        self, provider: IFileStorageProvider
    ) -> CliBuilder:
        """Set a custom file storage provider for the filesystem API."""
        self._file_storage_provider = provider
        self.add_installed_plugin("qodalis-cli-server-plugin-filesystem")
        self.add_installed_plugin(provider)
        return self

    @property
    def file_storage_provider(self) -> IFileStorageProvider | None:
        """Return the configured file storage provider, if any."""
        return self._file_storage_provider

    @property
    def filesystem_options(self) -> FileSystemOptions | None:
        """Return the legacy filesystem options, if configured."""
        return self._filesystem_options

    def add_data_explorer_provider(
        self,
        provider: IDataExplorerProvider,
        options: DataExplorerProviderOptions,
    ) -> CliBuilder:
        """Register a data explorer provider with its options (the data explorer plugin's rules applied, when it is
        loaded: ``data-explorer.policy``)."""
        self._data_explorer_registrations.append(
            _DataExplorerRegistration(provider, apply_data_explorer_policy(options, self.plugin_services.get(DATA_EXPLORER_POLICY_SERVICE)))
        )
        self.add_installed_plugin("qodalis-cli-server-plugin-data-explorer")
        self.add_installed_plugin(provider)
        return self

    @property
    def data_explorer_registrations(self) -> list[_DataExplorerRegistration]:
        """Return all registered data explorer provider registrations."""
        return list(self._data_explorer_registrations)

    @property
    def registry(self) -> CliCommandRegistry:
        """Return the underlying command registry."""
        return self._registry
