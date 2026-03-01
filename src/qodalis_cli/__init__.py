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
)
from .controllers import create_cli_router, create_cli_router_v2, create_cli_version_router
from .extensions import CliBuilder
from .processors import (
    CliEchoCommandProcessor,
    CliStatusCommandProcessor,
    CliSystemCommandProcessor,
    CliHttpCommandProcessor,
    CliHashCommandProcessor,
    CliBase64CommandProcessor,
    CliUuidCommandProcessor,
)
from .create_cli_server import create_cli_server, CliServerOptions

__all__ = [
    "ICliCommandAuthor",
    "CliCommandAuthor",
    "DEFAULT_LIBRARY_AUTHOR",
    "CliProcessCommand",
    "ICliCommandParameterDescriptor",
    "CliCommandParameterDescriptor",
    "ICliCommandProcessor",
    "CliCommandProcessor",
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
    "create_cli_router",
    "create_cli_router_v2",
    "create_cli_version_router",
    "CliBuilder",
    "CliEchoCommandProcessor",
    "CliStatusCommandProcessor",
    "CliSystemCommandProcessor",
    "CliHttpCommandProcessor",
    "CliHashCommandProcessor",
    "CliBase64CommandProcessor",
    "CliUuidCommandProcessor",
    "create_cli_server",
    "CliServerOptions",
]
