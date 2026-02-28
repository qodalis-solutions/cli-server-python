from __future__ import annotations

from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
)


class CliHelloCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "hello"

    @property
    def description(self) -> str:
        return "Greets the user"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="name",
                description="Name to greet",
                required=False,
                type="string",
                aliases=["-n"],
                default_value="World",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        name = command.args.get("name", command.value or "World")
        return f"Hello, {name}!"
