import asyncio
from qodalis_cli import CliCommandProcessor, CliProcessCommand
from qodalis_cli.processors.cli_echo_command_processor import join_shell_words


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
        # `echo "a" "b"` prints a b, as the browser's echo does
        text = join_shell_words(text)
        return text if text else "Usage: echo <text>"
