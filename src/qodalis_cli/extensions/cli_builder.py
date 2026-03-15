from __future__ import annotations

from typing import TYPE_CHECKING

from ..abstractions import ICliCommandProcessor, ICliModule, ICliJob, CliJobOptions, ICliJobStorageProvider
from ..filesystem import FileSystemOptions
from ..services.cli_command_registry import CliCommandRegistry

if TYPE_CHECKING:
    from qodalis_cli_filesystem import IFileStorageProvider


class CliBuilder:
    def __init__(self, registry: CliCommandRegistry) -> None:
        self._registry = registry
        self._filesystem_options: FileSystemOptions | None = None
        self._file_storage_provider: IFileStorageProvider | None = None
        self._job_registrations: list[tuple[ICliJob, CliJobOptions]] = []
        self._job_storage_provider: ICliJobStorageProvider | None = None

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

    def add_job(self, job: ICliJob, options: CliJobOptions) -> CliBuilder:
        """Register a background job with the given options."""
        self._job_registrations.append((job, options))
        return self

    def set_job_storage_provider(self, provider: ICliJobStorageProvider) -> CliBuilder:
        """Set a custom storage provider for job execution history."""
        self._job_storage_provider = provider
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

    @property
    def job_registrations(self) -> list[tuple[ICliJob, CliJobOptions]]:
        return self._job_registrations

    @property
    def job_storage_provider(self) -> ICliJobStorageProvider | None:
        return self._job_storage_provider
