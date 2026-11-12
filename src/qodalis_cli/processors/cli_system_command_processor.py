from __future__ import annotations

import asyncio
import os
import platform
import time

from ..abstractions import CliCommandProcessor, CliProcessCommand
from ..abstractions import CliCommandExample

_start_time = time.time()


class CliSystemCommandProcessor(CliCommandProcessor):
    """Command processor that reports server system information."""

    @property
    def command(self) -> str:
        return "system"

    @property
    def description(self) -> str:
        return "Shows server system information"

    @property
    def examples(self) -> list[CliCommandExample]:
        return [
            CliCommandExample("system", "Operating system, CPU and memory of the server"),
        ]

    @property
    def documentation(self) -> str:
        return (
            "Shows the operating system, CPU, memory and runtime of the machine the server runs on."
        )

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        uptime_secs = int(time.time() - _start_time)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)

        lines = [
            f"Hostname:      {platform.node()}",
            f"OS:            {platform.system()} {platform.release()}",
            f"Architecture:  {platform.machine()}",
            f"CPU Cores:     {os.cpu_count()}",
            f"Python:        {platform.python_version()}",
            f"Server Uptime: {hours}h {minutes}m {seconds}s",
        ]
        return "\n".join(lines)
