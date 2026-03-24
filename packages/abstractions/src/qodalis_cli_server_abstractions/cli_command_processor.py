from __future__ import annotations

import abc
import asyncio
from typing import TYPE_CHECKING, Any

from .cli_command_author import DEFAULT_LIBRARY_AUTHOR, ICliCommandAuthor

if TYPE_CHECKING:
    from .cli_command_parameter_descriptor import ICliCommandParameterDescriptor
    from .cli_process_command import CliProcessCommand


class ICliCommandProcessor(abc.ABC):
    """Abstract base class for all CLI command processors."""

    @property
    @abc.abstractmethod
    def command(self) -> str:
        """The primary command name that triggers this processor."""
        ...

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Short human-readable description of the command."""
        ...

    @property
    def author(self) -> ICliCommandAuthor:
        """Author of this command processor."""
        return DEFAULT_LIBRARY_AUTHOR

    @property
    def version(self) -> str:
        """Semantic version of this processor."""
        return "1.0.0"

    @property
    def allow_unlisted_commands(self) -> bool | None:
        """Whether unrecognized sub-commands should be forwarded to this processor."""
        return None

    @property
    def value_required(self) -> bool | None:
        """Whether the command requires a positional value argument."""
        return None

    @property
    def processors(self) -> list[ICliCommandProcessor] | None:
        """Optional nested sub-command processors."""
        return None

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor] | None:
        """Optional parameter descriptors for this command."""
        return None

    @abc.abstractmethod
    async def handle_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> str:
        """Execute the command and return a plain-text result.

        Args:
            command: The parsed command to execute.
            cancellation_event: Optional event that is set when the caller
                requests cancellation. Processors should check
                ``cancellation_event.is_set()`` periodically for long-running
                operations.

        Returns:
            A string containing the command output.
        """
        ...

    _STRUCTURED_NOT_IMPLEMENTED = object()

    async def handle_structured_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> Any:
        """Execute the command and return a structured response.

        Override this to return a ``CliServerResponse`` directly, bypassing
        the default text wrapping. Return ``_STRUCTURED_NOT_IMPLEMENTED``
        to fall back to ``handle_async``.

        Args:
            command: The parsed command to execute.
            cancellation_event: Optional cancellation event (forwarded to
                ``handle_async`` when the default implementation falls back).

        Returns:
            A ``CliServerResponse`` or the sentinel to indicate fallback.
        """
        return self._STRUCTURED_NOT_IMPLEMENTED


class CliCommandProcessor(ICliCommandProcessor):
    """Convenience base class for command processors with sensible defaults."""

    pass
