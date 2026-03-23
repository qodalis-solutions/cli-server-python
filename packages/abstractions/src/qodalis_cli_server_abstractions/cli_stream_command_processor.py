from __future__ import annotations

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
    ) -> int:
        """Execute the command, calling *emit* for each output chunk.

        Returns the exit code (0 for success).
        """
        ...


def is_stream_capable(processor: object) -> bool:
    """Return True if *processor* implements streaming."""
    return isinstance(processor, ICliStreamCommandProcessor)
