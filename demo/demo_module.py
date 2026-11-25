"""The demo's own example commands as one module."""

from __future__ import annotations

from typing import Sequence

from qodalis_cli_server_abstractions import CliModule, ICliCommandProcessor

from processors import (
    CliBase64CommandProcessor,
    CliEchoCommandProcessor,
    CliHashCommandProcessor,
    CliHelloCommandProcessor,
    CliMathCommandProcessor,
    CliStatusCommandProcessor,
    CliSystemCommandProcessor,
    CliTimeCommandProcessor,
)


class DemoModule(CliModule):
    """The demo's own example commands (echo, status, time, hello, math, system, hash, base64): ``GET /commands``
    lists them with ``module: "demo"`` and the dashboard's plugins page shows them as the demo
    plugin (enable/disable)."""

    def __init__(self) -> None:
        self._processors: list[ICliCommandProcessor] = [
            CliEchoCommandProcessor(),
            CliStatusCommandProcessor(),
            CliTimeCommandProcessor(),
            CliHelloCommandProcessor(),
            CliMathCommandProcessor(),
            CliSystemCommandProcessor(),
            CliHashCommandProcessor(),
            CliBase64CommandProcessor(),
        ]

    @property
    def name(self) -> str:
        return "demo"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Demo commands: examples of server-side commands"

    @property
    def icon(self) -> str | None:
        return "🧪"

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        return self._processors
