"""Qodalis CLI Server - A Python CLI server framework for the Qodalis CLI ecosystem."""

from .abstractions import (
    ICliCommandAuthor,
    CliCommandAuthor,
    DEFAULT_LIBRARY_AUTHOR,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    CliCommandParameterDescriptor,
    ICliCommandProcessor,
    CliCommandProcessor,
    ICliModule,
    CliModule,
    ICliProcessorFilter,
)
from .models import (
    CliServerOutput,
    CliServerResponse,
    CliServerCommandDescriptor,
    CliServerCommandParameterDescriptorDto,
)
from .services import (
    ICliCommandRegistry,
    CliCommandRegistry,
    ICliResponseBuilder,
    CliResponseBuilder,
    ICliCommandExecutorService,
    CliCommandExecutorService,
    CliEventSocketManager,
    CliShellSessionManager,
)
from .controllers import create_cli_router, create_cli_router_v2, create_cli_version_router, create_filesystem_router
from .extensions import CliBuilder
from .filesystem import FileSystemOptions, FileSystemPathValidator

# Re-export filesystem plugin types for convenience
from qodalis_cli_filesystem import (
    FileEntry,
    FileStat,
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
    IFileStorageProvider,
)
from qodalis_cli_filesystem.providers import (
    InMemoryFileStorageProvider,
    OsFileStorageProvider,
    OsProviderOptions,
)
from .processors import (
    CliEchoCommandProcessor,
    CliStatusCommandProcessor,
    CliSystemCommandProcessor,
    CliHttpCommandProcessor,
    CliHashCommandProcessor,
    CliBase64CommandProcessor,
)
from .create_cli_server import create_cli_server, CliServerOptions, CliServerResult

__all__ = [
    "ICliCommandAuthor",
    "CliCommandAuthor",
    "DEFAULT_LIBRARY_AUTHOR",
    "CliProcessCommand",
    "ICliCommandParameterDescriptor",
    "CliCommandParameterDescriptor",
    "ICliCommandProcessor",
    "CliCommandProcessor",
    "ICliModule",
    "CliModule",
    "ICliProcessorFilter",
    "CliServerOutput",
    "CliServerResponse",
    "CliServerCommandDescriptor",
    "CliServerCommandParameterDescriptorDto",
    "ICliCommandRegistry",
    "CliCommandRegistry",
    "ICliResponseBuilder",
    "CliResponseBuilder",
    "ICliCommandExecutorService",
    "CliCommandExecutorService",
    "CliEventSocketManager",
    "CliShellSessionManager",
    "create_cli_router",
    "create_cli_router_v2",
    "create_cli_version_router",
    "create_filesystem_router",
    "CliBuilder",
    "FileSystemOptions",
    "FileSystemPathValidator",
    "CliEchoCommandProcessor",
    "CliStatusCommandProcessor",
    "CliSystemCommandProcessor",
    "CliHttpCommandProcessor",
    "CliHashCommandProcessor",
    "CliBase64CommandProcessor",
    "create_cli_server",
    "CliServerOptions",
    "CliServerResult",
    "FileEntry",
    "FileStat",
    "FileStorageExistsError",
    "FileStorageIsADirectoryError",
    "FileStorageNotADirectoryError",
    "FileStorageNotFoundError",
    "FileStoragePermissionError",
    "IFileStorageProvider",
    "InMemoryFileStorageProvider",
    "OsFileStorageProvider",
    "OsProviderOptions",
]
