from __future__ import annotations

import asyncio

from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)


class _CliMathAddProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "Adds two numbers"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="a", description="First number", required=True, type="number"),
            CliCommandParameterDescriptor(name="b", description="Second number", required=True, type="number"),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        a = float(command.args.get("a", 0))
        b = float(command.args.get("b", 0))
        result = a + b
        return str(int(result)) if result == int(result) else str(result)


class _CliMathMultiplyProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "multiply"

    @property
    def description(self) -> str:
        return "Multiplies two numbers"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="a", description="First number", required=True, type="number"),
            CliCommandParameterDescriptor(name="b", description="Second number", required=True, type="number"),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        a = float(command.args.get("a", 0))
        b = float(command.args.get("b", 0))
        result = a * b
        return str(int(result)) if result == int(result) else str(result)


class CliMathCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "math"

    @property
    def description(self) -> str:
        return "Performs basic math operations"

    @property
    def allow_unlisted_commands(self) -> bool:
        return False

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_CliMathAddProcessor(), _CliMathMultiplyProcessor()]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "Usage: math add|multiply --a <number> --b <number>"
