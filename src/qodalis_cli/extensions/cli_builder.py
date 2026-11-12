from __future__ import annotations

from typing import TYPE_CHECKING

from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    IDataExplorerProvider,
)

from ..abstractions import ICliCommandProcessor, ICliModule
from ..filesystem import FileSystemOptions
from ..models.cli_installed_plugin import (
    CORE_DISTRIBUTION,
    CliInstalledPlugin,
    distribution_of,
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
        self.add_installed_plugin(CORE_DISTRIBUTION)

    def add_installed_plugin(
        self,
        plugin: str | object,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> CliBuilder:
        """List a server plugin package among the installed plugins (once,
        in the order added), shown on the admin dashboard's plugins page.

        ``plugin`` is a distribution name (``qodalis-cli-server-plugin-users``)
        or an object (module, class or instance) of the package.  The built-in
        plugins list themselves when added or mounted; call it from a plugin of
        your own.  The host application's own code (no distribution) is never
        listed.
        """
        distribution = plugin if isinstance(plugin, str) else distribution_of(plugin)
        if not distribution or any(p.id == distribution for p in self._installed_plugins):
            return self
        self._installed_plugins.append(
            CliInstalledPlugin.from_distribution(distribution, name=name, description=description)
        )
        return self

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
