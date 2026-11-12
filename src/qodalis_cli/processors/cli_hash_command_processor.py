from __future__ import annotations

import asyncio
import hashlib

from ..abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    get_positional_text,
)
from ..abstractions import CliCommandExample

SUPPORTED_ALGORITHMS = ["md5", "sha1", "sha256", "sha512"]


class CliHashCommandProcessor(CliCommandProcessor):
    """Command processor that computes cryptographic hashes of input text."""

    @property
    def command(self) -> str:
        return "hash"

    @property
    def value_required(self) -> bool | None:
        # Takes an optional positional value (exposed as the MCP tool's ``value``).
        return False

    @property
    def description(self) -> str:
        return "Computes hash of the input text"

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("hash hello", "SHA-256 digest of hello"),
            CliCommandExample("hash hello --algorithm md5", "MD5 digest of hello"),
        ]

    @property
    def documentation(self) -> str:
        return (
            "Computes a digest of the text on the server and prints it in hexadecimal. The default algorithm is sha256."
        )

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="algorithm",
                description=f"Hash algorithm ({', '.join(SUPPORTED_ALGORITHMS)})",
                aliases=["-a"],
                default_value="sha256",
                type="string",
                allowed_values=list(SUPPORTED_ALGORITHMS),
            ),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        text = get_positional_text(command)
        if not text:
            return "Usage: hash <text> [--algorithm sha256]"

        algo = command.args.get("algorithm", "sha256").lower()
        if algo not in SUPPORTED_ALGORITHMS:
            return f"Unsupported algorithm: {algo}. Supported: {', '.join(SUPPORTED_ALGORITHMS)}"

        h = hashlib.new(algo)
        h.update(text.encode("utf-8"))
        digest = h.hexdigest()

        return f"{algo}: {digest}"
