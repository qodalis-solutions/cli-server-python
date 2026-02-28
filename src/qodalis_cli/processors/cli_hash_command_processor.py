from __future__ import annotations

import hashlib

from ..abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
)

SUPPORTED_ALGORITHMS = ["md5", "sha1", "sha256", "sha512"]


class CliHashCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "hash"

    @property
    def description(self) -> str:
        return "Computes hash of the input text"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="algorithm",
                description=f"Hash algorithm ({', '.join(SUPPORTED_ALGORITHMS)})",
                aliases=["-a"],
                default_value="sha256",
                type="string",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        text = command.value
        if not text:
            return "Usage: hash <text> [--algorithm sha256]"

        algo = command.args.get("algorithm", "sha256").lower()
        if algo not in SUPPORTED_ALGORITHMS:
            return f"Unsupported algorithm: {algo}. Supported: {', '.join(SUPPORTED_ALGORITHMS)}"

        h = hashlib.new(algo)
        h.update(text.encode("utf-8"))
        digest = h.hexdigest()

        return f"{algo}: {digest}"
