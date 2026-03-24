from __future__ import annotations

import asyncio

from ..abstractions import CliCommandProcessor, CliProcessCommand


class CliEchoCommandProcessor(CliCommandProcessor):
    """Command processor that echoes the input text back."""

    @property
    def command(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes the input text back"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = command.value or ""
        return text if text else "Usage: echo <text>"
