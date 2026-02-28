from __future__ import annotations

import abc

from ..abstractions import CliProcessCommand
from ..models.cli_server_output import CliServerTextOutput
from ..models.cli_server_response import CliServerResponse
from .cli_command_registry import ICliCommandRegistry


class ICliCommandExecutorService(abc.ABC):
    @abc.abstractmethod
    async def execute_async(self, command: CliProcessCommand) -> CliServerResponse: ...


class CliCommandExecutorService(ICliCommandExecutorService):
    def __init__(self, registry: ICliCommandRegistry) -> None:
        self._registry = registry

    async def execute_async(self, command: CliProcessCommand) -> CliServerResponse:
        chain = command.chain_commands if command.chain_commands else None
        processor = self._registry.find_processor(command.command, chain)

        if processor is None:
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Unknown command: {command.command}",
                        style="error",
                    )
                ],
            )

        try:
            result = await processor.handle_async(command)
            return CliServerResponse(
                exitCode=0,
                outputs=[CliServerTextOutput(value=result)],
            )
        except Exception as exc:
            return CliServerResponse(
                exitCode=1,
                outputs=[
                    CliServerTextOutput(
                        value=f"Error executing command: {exc}",
                        style="error",
                    )
                ],
            )
