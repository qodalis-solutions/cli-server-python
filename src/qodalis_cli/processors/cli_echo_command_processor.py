from __future__ import annotations

import asyncio

from ..abstractions import (
    CliCommandProcessor,
    CliProcessCommand,
    get_positional_text,
    join_shell_words,
    split_shell_words,
)
from ..abstractions import CliCommandExample

__all__ = ["CliEchoCommandProcessor", "join_shell_words", "split_shell_words"]


class CliEchoCommandProcessor(CliCommandProcessor):
    """Command processor that echoes the input text back, as a shell's echo prints it."""

    @property
    def command(self) -> str:
        return "echo"

    @property
    def value_required(self) -> bool | None:
        # Takes an optional positional value (exposed as the MCP tool's ``value``).
        return False

    @property
    def description(self) -> str:
        return "Echoes the input text back"

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("echo hello world", "Prints hello world"),
        ]

    @property
    def documentation(self) -> str:
        return (
            "Prints its input back as the server received it. Quoted words keep their spaces: echo \"a  b\" prints a  b."
        )

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = get_positional_text(command)
        return text if text else "Usage: echo <text>"
