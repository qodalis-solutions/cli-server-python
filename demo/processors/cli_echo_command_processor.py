import asyncio
from qodalis_cli import CliCommandProcessor, CliProcessCommand


class CliEchoCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "echo"

    @property
    def value_required(self) -> bool | None:
        # Optional positional value (the MCP tool's ``value`` argument).
        return False

    @property
    def description(self) -> str:
        return "Echoes the input text back"

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = command.value or ""
        return text if text else "Usage: echo <text>"
