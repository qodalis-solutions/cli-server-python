from __future__ import annotations

import asyncio

from ..abstractions import CliCommandProcessor, CliProcessCommand


def join_shell_words(text: str) -> str:
    """The arguments as a shell's echo prints them, as the browser CLI's own
    ``echo`` does: quotes removed, the words joined by one space, spacing inside
    quotes kept (``"a" "b"`` -> ``a b``). A quote without a closing one is text."""
    words: list[str] = []
    current = ""
    in_word = False
    i = 0
    while i < len(text):
        ch = text[i]
        end = text.find(ch, i + 1) if ch in ('"', "'") else -1
        if end != -1:
            current += text[i + 1 : end]
            in_word = True
            i = end
        elif ch.isspace():
            if in_word:
                words.append(current)
            current = ""
            in_word = False
        else:
            current += ch
            in_word = True
        i += 1
    if in_word:
        words.append(current)
    return " ".join(words)


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

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = join_shell_words(command.value or "")
        return text if text else "Usage: echo <text>"
