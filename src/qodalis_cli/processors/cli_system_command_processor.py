from __future__ import annotations

import os
import platform
import time

from ..abstractions import CliCommandProcessor, CliProcessCommand

_start_time = time.time()


class CliSystemCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "system"

    @property
    def description(self) -> str:
        return "Shows server system information"

    async def handle_async(self, command: CliProcessCommand) -> str:
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
