from __future__ import annotations

import abc
import logging
from typing import Sequence

from ..abstractions import CliProcessCommand, ICliProcessorFilter
from ..models.cli_server_output import CliServerTextOutput
from ..models.cli_server_response import CliServerResponse
from .cli_command_registry import ICliCommandRegistry

logger = logging.getLogger(__name__)


class ICliCommandExecutorService(abc.ABC):
    """Interface for a service that executes parsed CLI commands."""

    @abc.abstractmethod
    async def execute_async(self, command: CliProcessCommand) -> CliServerResponse:
        """Execute a command and return a structured response.

        Args:
            command: The parsed command to execute.

        Returns:
            A ``CliServerResponse`` containing outputs and an exit code.
        """
        ...


class CliCommandExecutorService(ICliCommandExecutorService):
    """Default executor that resolves processors from a registry and runs them."""

    def __init__(
        self,
        registry: ICliCommandRegistry,
        filters: Sequence[ICliProcessorFilter] | None = None,
    ) -> None:
        self._registry = registry
        self._filters: list[ICliProcessorFilter] = list(filters or [])

    def add_filter(self, filter_: ICliProcessorFilter) -> None:
        """Register an additional processor filter at runtime.

        Args:
            filter_: The filter to add.
        """
        self._filters.append(filter_)

    async def execute_async(self, command: CliProcessCommand) -> CliServerResponse:
        chain = command.chain_commands if command.chain_commands else None
        full_command = (
            f"{command.command} {' '.join(chain)}" if chain else command.command
        )

        logger.info("Executing command: %s", full_command)

        processor = self._registry.find_processor(command.command, chain)

        if processor is None:
            logger.warning("Unknown command: %s", full_command)
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Unknown command: {command.command}",
                        style="error",
                    )
                ],
            )

        if any(not f.is_allowed(processor) for f in self._filters):
            logger.warning(
                "Command blocked by filter (plugin disabled): %s", full_command
            )
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Command '{command.command}' is currently disabled.",
                        style="error",
                    )
                ],
            )

        try:
            structured = await processor.handle_structured_async(command)
            if structured is not processor._STRUCTURED_NOT_IMPLEMENTED:
                logger.info("Command completed (structured): %s", full_command)
                return structured
            result = await processor.handle_async(command)
            logger.info("Command completed: %s", full_command)
            return CliServerResponse(
                exitCode=0,
                outputs=[CliServerTextOutput(value=result)],
            )
        except Exception as exc:
            logger.error("Command failed: %s — %s", full_command, exc, exc_info=True)
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Error executing command: {exc}",
                        style="error",
                    )
                ],
            )
