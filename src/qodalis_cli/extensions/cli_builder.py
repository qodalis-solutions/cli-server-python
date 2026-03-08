from __future__ import annotations

from typing import TYPE_CHECKING

from ..abstractions import ICliCommandProcessor, ICliModule
from ..filesystem import FileSystemOptions
from ..services.cli_command_registry import CliCommandRegistry

if TYPE_CHECKING:
    from plugins.filesystem import IFileStorageProvider


class CliBuilder:
    def __init__(self, registry: CliCommandRegistry) -> None:
        self._registry = registry
        self._filesystem_options: FileSystemOptions | None = None
        self._file_storage_provider: IFileStorageProvider | None = None

    def add_processor(self, processor: ICliCommandProcessor) -> CliBuilder:
        self._registry.register(processor)
        return self

    def add_module(self, module: ICliModule) -> CliBuilder:
        for processor in module.processors:
            self._registry.register(processor)
        return self

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
        return self._filesystem_options

    @property
    def registry(self) -> CliCommandRegistry:
        return self._registry
