from __future__ import annotations

import asyncio
import platform
import sys

from ..abstractions import CliCommandProcessor, CliProcessCommand
from ..abstractions import CliCommandExample


class CliStatusCommandProcessor(CliCommandProcessor):
    """Command processor that reports server status information."""

    @property
    def command(self) -> str:
        return "status"

    @property
    def description(self) -> str:
        return "Shows server status information"

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("status", "Uptime, version and state of the server"),
        ]

    @property
    def documentation(self) -> str:
        return (
            "Shows how long the server has been running, its version and its current state."
        )

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return (
            f"Server: Qodalis CLI Server (Python)\n"
            f"Python: {sys.version.split()[0]}\n"
            f"Platform: {platform.system()} {platform.release()}\n"
            f"Status: Running"
        )
