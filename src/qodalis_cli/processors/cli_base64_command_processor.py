from __future__ import annotations

import asyncio
import base64

from ..abstractions import CliCommandProcessor, CliProcessCommand, ICliCommandProcessor, get_positional_text
from ..abstractions import CliCommandExample


class _Base64EncodeProcessor(CliCommandProcessor):
    """Sub-processor that encodes text to Base64."""

    @property
    def command(self) -> str:
        return "encode"

    @property
    def value_required(self) -> bool | None:
        # Takes an optional positional value (exposed as the MCP tool's ``value``).
        return False

    @property
    def description(self) -> str:
        return "Encodes text to Base64"

    @property
    def aliases(self) -> list[str]:
        return ["enc"]

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("base64 encode hello", "Prints aGVsbG8="),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = get_positional_text(command)
        if not text:
            return "Usage: base64 encode <text>"
        return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _Base64DecodeProcessor(CliCommandProcessor):
    """Sub-processor that decodes Base64 to text."""

    @property
    def command(self) -> str:
        return "decode"

    @property
    def value_required(self) -> bool | None:
        # Takes an optional positional value (exposed as the MCP tool's ``value``).
        return False

    @property
    def description(self) -> str:
        return "Decodes Base64 to text"

    @property
    def aliases(self) -> list[str]:
        return ["dec"]

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("base64 decode aGVsbG8=", "Prints hello"),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = get_positional_text(command)
        if not text:
            return "Usage: base64 decode <base64string>"
        try:
            return base64.b64decode(text).decode("utf-8")
        except Exception:
            return "Error: Invalid Base64 input"


class CliBase64CommandProcessor(CliCommandProcessor):
    """Command processor for Base64 encoding and decoding."""

    @property
    def command(self) -> str:
        return "base64"

    @property
    def description(self) -> str:
        return "Encodes or decodes Base64 text"

    @property
    def aliases(self) -> list[str]:
        return ["b64"]

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("base64 encode hello", "Prints aGVsbG8="),
            CliCommandExample("base64 decode aGVsbG8=", "Prints hello"),
        ]

    @property
    def documentation(self) -> str:
        return (
            "Encodes text to Base64 or decodes Base64 back to text (UTF-8), on the server."
        )

    @property
    def allow_unlisted_commands(self) -> bool:
        return False

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_Base64EncodeProcessor(), _Base64DecodeProcessor()]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "Usage: base64 encode|decode <text>"
