from __future__ import annotations

from typing import Sequence

from qodalis_cli_server_abstractions import (
    CliModule,
    ICliCommandProcessor,
)

from .permissions import AWS_PERMISSION_ENTRIES
from .processors.aws_command_processor import AwsCommandProcessor


class AwsModule(CliModule):
    """AWS cloud resource management module."""

    def __init__(self) -> None:
        # One processor (and config service) per module: the plugin's settings configure it
        self._processors: list[ICliCommandProcessor] = [AwsCommandProcessor()]

    @property
    def name(self) -> str:
        """Returns the module name."""
        return "aws"

    @property
    def version(self) -> str:
        """Returns the module version."""
        return "1.0.0"

    @property
    def description(self) -> str:
        """Returns a human-readable description of the module."""
        return "Provides AWS cloud resource management commands"

    @property
    def icon(self) -> str | None:
        """One emoji or an image URL shown for the module."""
        return "☁️"

    @property
    def permissions(self) -> list[dict[str, str]]:
        """``aws:read`` and ``aws:write``, added to the server's permission catalog (group AWS)."""
        return [dict(e) for e in AWS_PERMISSION_ENTRIES]

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        """Returns the command processors provided by this module."""
        return self._processors
