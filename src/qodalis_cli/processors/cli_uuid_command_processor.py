from __future__ import annotations

import uuid

from ..abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
)


class CliUuidCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "uuid"

    @property
    def description(self) -> str:
        return "Generates random UUIDs"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="count",
                description="Number of UUIDs to generate (max 50)",
                aliases=["-n"],
                default_value="1",
                type="number",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        count = int(command.args.get("count", 1))
        count = max(1, min(count, 50))

        uuids = [str(uuid.uuid4()) for _ in range(count)]
        return "\n".join(uuids)
