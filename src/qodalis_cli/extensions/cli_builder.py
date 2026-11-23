from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Mapping

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
        distribution = plugin if isinstance(plugin, str) else distribution_of(plugin)
        if not distribution or any(p.id == distribution for p in self._installed_plugins):
            return self
        self._installed_plugins.append(
            CliInstalledPlugin.from_distribution(
                distribution, name=name, description=description, icon=icon
            )
        )
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
            self._registry.register(processor)
        return self

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
        """Register a data explorer provider with its options."""
        self._data_explorer_registrations.append(
            _DataExplorerRegistration(provider, options)
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
