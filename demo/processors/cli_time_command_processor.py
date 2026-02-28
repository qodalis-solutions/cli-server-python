from __future__ import annotations

from datetime import datetime, timezone

from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
)


class CliTimeCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "time"

    @property
    def description(self) -> str:
        return "Shows the current server date and time"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="utc",
                description="Show time in UTC",
                required=False,
                type="boolean",
            ),
            CliCommandParameterDescriptor(
                name="format",
                description="Date/time format string",
                required=False,
                type="string",
                aliases=["-f"],
                default_value="%Y-%m-%d %H:%M:%S",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        use_utc = "utc" in command.args
        fmt = command.args.get("format", "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc) if use_utc else datetime.now()
        label = "UTC" if use_utc else "Local"
        return f"{label}: {now.strftime(fmt)}"
