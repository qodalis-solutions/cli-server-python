import asyncio
import platform
import sys

from qodalis_cli import CliCommandProcessor, CliProcessCommand


class CliStatusCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "status"

    @property
    def description(self) -> str:
        return "Shows server status information"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return (
            f"Server: Qodalis CLI Server Demo (Python)\n"
            f"Python: {sys.version.split()[0]}\n"
            f"Platform: {platform.system()} {platform.release()}\n"
            f"Status: Running"
        )
