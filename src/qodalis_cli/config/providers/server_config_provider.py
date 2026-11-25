"""``server``: the server process (read-only)."""

from __future__ import annotations

import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone

from qodalis_cli_server_abstractions.config import (
    CliConfigEntry,
    CliConfigSection,
    ICliConfigProvider,
    format_cli_config_duration,
)


class CliServerConfigProvider(ICliConfigProvider):
    """``server`` (read-only): the server process: platform, Python version,
    server version, environment, process id, host name, OS, start time and
    uptime."""

    id = "server"
    title = "Server"
    description = "The server process (read-only)"
    order = 0

    def __init__(self, version: str) -> None:
        self.version = version
        self._started = time.time()

    def get_sections(self, context: object = None) -> list[CliConfigSection]:
        uptime_seconds = int(time.time() - self._started)
        env = os.environ.get("QCLI_ENV") or os.environ.get("PYTHON_ENV") or os.environ.get("ENVIRONMENT")
        started_at = datetime.fromtimestamp(self._started, tz=timezone.utc).isoformat(timespec="milliseconds")
        return [CliConfigSection(id="runtime", title="Runtime", entries=[
            CliConfigEntry(key="platform", label="Platform", description="Server platform", type="string", value="python"),
            CliConfigEntry(
                key="platformVersion", label="Platform version", description="Python version",
                type="string", value=sys.version.split()[0],
            ),
            CliConfigEntry(
                key="version", label="Server version", description="Version of qodalis-cli-server",
                type="string", value=self.version,
            ),
            CliConfigEntry(
                key="environment", label="Environment", description="QCLI_ENV, PYTHON_ENV or ENVIRONMENT",
                type="string", value=env or "development", source="environment" if env else "default",
            ),
            CliConfigEntry(key="pid", label="Process id", description="Process ID", type="number", value=os.getpid()),
            CliConfigEntry(
                key="hostname", label="Host name", description="Name of the machine",
                type="string", value=socket.gethostname(),
            ),
            CliConfigEntry(
                key="os", label="Operating system", description="OS platform and architecture",
                type="string", value=f"{platform.system().lower()} {platform.machine()}",
            ),
            CliConfigEntry(
                key="startedAt", label="Started at", description="When the server started (UTC)",
                type="string", value=started_at.replace("+00:00", "Z"),
            ),
            CliConfigEntry(
                key="uptime", label="Uptime", description="Time since the server started",
                type="duration", value=format_cli_config_duration(uptime_seconds * 1000),
            ),
        ])]
