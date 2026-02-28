from __future__ import annotations

import base64

from ..abstractions import CliCommandProcessor, CliProcessCommand, ICliCommandProcessor


class _Base64EncodeProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "encode"

    @property
    def description(self) -> str:
        return "Encodes text to Base64"

    async def handle_async(self, command: CliProcessCommand) -> str:
        text = command.value
        if not text:
            return "Usage: base64 encode <text>"
        return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _Base64DecodeProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "decode"

    @property
    def description(self) -> str:
        return "Decodes Base64 to text"

    async def handle_async(self, command: CliProcessCommand) -> str:
        text = command.value
        if not text:
            return "Usage: base64 decode <base64string>"
        try:
            return base64.b64decode(text).decode("utf-8")
        except Exception:
            return "Error: Invalid Base64 input"


class CliBase64CommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "base64"

    @property
    def description(self) -> str:
        return "Encodes or decodes Base64 text"

    @property
    def allow_unlisted_commands(self) -> bool:
        return False

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_Base64EncodeProcessor(), _Base64DecodeProcessor()]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Usage: base64 encode|decode <text>"
