from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .cli_command_author import DEFAULT_LIBRARY_AUTHOR, ICliCommandAuthor

if TYPE_CHECKING:
    from .cli_command_parameter_descriptor import ICliCommandParameterDescriptor
    from .cli_process_command import CliProcessCommand


@dataclass(frozen=True)
class CliCommandExample:
    """An example command line shown in a command's help."""

    command: str
    description: str | None = None


def command_matches(processor: Any, name: str) -> bool:
    """Whether ``name`` is the processor's command or one of its aliases
    (ignoring case)."""
    lower = name.lower()
    if processor.command.lower() == lower:
        return True
    return any(a.lower() == lower for a in (getattr(processor, "aliases", None) or []))


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
    def aliases(self) -> list[str] | None:
        """Other names of the command (root or sub-command).  The server
        resolves them like the command itself, for execution and
        permissions."""
        return None

    @property
    def examples(self) -> list[CliCommandExample] | None:
        """Example command lines for the command's help."""
        return None

    @property
    def documentation(self) -> str | None:
        """Longer help text for ``help <command>``: plain text, paragraphs
        separated by a blank line."""
        return None

    @property
    def module(self) -> str | None:
        """The group the command is listed under in the web CLI's help."""
        return None

    @property
    def icon(self) -> str | None:
        """One emoji shown next to the command in the web CLI's help."""
        return None

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

    @property
    def required_permissions(self) -> list[str] | None:
        """Permissions (``resource:action``) a caller must hold to execute
        this command.  Enforced server-side when the server is protected by
        the users plugin; ignored otherwise."""
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
