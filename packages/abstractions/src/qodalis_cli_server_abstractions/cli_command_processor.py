from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from .cli_command_author import DEFAULT_LIBRARY_AUTHOR, ICliCommandAuthor

if TYPE_CHECKING:
    from .cli_command_parameter_descriptor import ICliCommandParameterDescriptor
    from .cli_process_command import CliProcessCommand


class ICliCommandProcessor(abc.ABC):
    @property
    @abc.abstractmethod
    def command(self) -> str: ...

    @property
    @abc.abstractmethod
    def description(self) -> str: ...

    @property
    def author(self) -> ICliCommandAuthor:
        return DEFAULT_LIBRARY_AUTHOR

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def allow_unlisted_commands(self) -> bool | None:
        return None

    @property
    def value_required(self) -> bool | None:
        return None

    @property
    def api_version(self) -> int:
        """API version this processor targets. Default 1 for backward compat."""
        return 1

    @property
    def processors(self) -> list[ICliCommandProcessor] | None:
        return None

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor] | None:
        return None

    @abc.abstractmethod
    async def handle_async(self, command: CliProcessCommand) -> str: ...


class CliCommandProcessor(ICliCommandProcessor):
    """Convenience base class with sensible defaults."""

    pass
