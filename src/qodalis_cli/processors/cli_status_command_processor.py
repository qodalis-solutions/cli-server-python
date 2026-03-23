from __future__ import annotations

import platform
import sys

from ..abstractions import CliCommandProcessor, CliProcessCommand


class CliStatusCommandProcessor(CliCommandProcessor):
    """Command processor that reports server status information."""

    @property
    def command(self) -> str:
        return "status"

    @property
    def description(self) -> str:
        return "Shows server status information"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return (
            f"Server: Qodalis CLI Server (Python)\n"
            f"Python: {sys.version.split()[0]}\n"
            f"Platform: {platform.system()} {platform.release()}\n"
            f"Status: Running"
        )
