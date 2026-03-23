from __future__ import annotations

from typing import TYPE_CHECKING

from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    IDataExplorerProvider,
)

from ..abstractions import ICliCommandProcessor, ICliModule
from ..filesystem import FileSystemOptions
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
        for processor in module.processors:
            self._registry.register(processor)
        return self

    @property
    def modules(self) -> list[ICliModule]:
        """Return a copy of all registered modules."""
        return list(self._modules)

    def add_filesystem(self, options: FileSystemOptions) -> CliBuilder:
        """Enable the filesystem API with the given options (legacy)."""
        self._filesystem_options = options
        return self

    def set_file_storage_provider(
        self, provider: IFileStorageProvider
    ) -> CliBuilder:
        """Set a custom file storage provider for the filesystem API."""
        self._file_storage_provider = provider
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
        return self

    @property
    def data_explorer_registrations(self) -> list[_DataExplorerRegistration]:
        """Return all registered data explorer provider registrations."""
        return list(self._data_explorer_registrations)

    @property
    def registry(self) -> CliCommandRegistry:
        """Return the underlying command registry."""
        return self._registry
