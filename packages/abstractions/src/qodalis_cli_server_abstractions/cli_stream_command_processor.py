from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Callable

from .cli_process_command import CliProcessCommand


class ICliStreamCommandProcessor(ABC):
    """Optional interface for processors that support streaming output.

    Processors implementing this can emit output chunks incrementally
    via the ``emit`` callback, enabling real-time rendering on the client.
    """

    @abstractmethod
    async def handle_stream_async(
        self,
        command: CliProcessCommand,
        emit: Callable[[dict[str, Any]], None],
        cancellation_event: asyncio.Event | None = None,
    ) -> int:
        """Execute the command, calling *emit* for each output chunk.

        Args:
            command: The parsed command to execute.
            emit: Callback invoked for each output chunk.
            cancellation_event: Optional event that is set when the caller
                requests cancellation. Processors should check
                ``cancellation_event.is_set()`` periodically for long-running
                operations.

        Returns the exit code (0 for success).
        """
        ...


def is_stream_capable(processor: object) -> bool:
    """Return True if *processor* implements streaming."""
    return isinstance(processor, ICliStreamCommandProcessor)
