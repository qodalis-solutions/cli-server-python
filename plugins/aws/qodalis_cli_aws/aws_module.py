from __future__ import annotations

from typing import Sequence

from qodalis_cli_server_abstractions import (
    CliModule,
    ICliCommandProcessor,
)

from .processors.aws_command_processor import AwsCommandProcessor


class AwsModule(CliModule):
    """AWS cloud resource management module."""

    @property
    def name(self) -> str:
        return "aws"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Provides AWS cloud resource management commands"

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        return [AwsCommandProcessor()]
