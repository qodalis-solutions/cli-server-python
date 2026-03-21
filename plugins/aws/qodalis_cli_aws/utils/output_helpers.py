from __future__ import annotations

import sys
import os
from typing import Any

_src_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'src')
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from qodalis_cli.models.cli_server_response import CliServerResponse
from qodalis_cli.models.cli_server_output import (
    CliServerTextOutput,
    CliServerTableOutput,
    CliServerListOutput,
    CliServerJsonOutput,
    CliServerKeyValueOutput,
    CliServerKeyValueEntry,
    CliServerOutput,
)

from qodalis_cli_server_abstractions import CliProcessCommand


def build_response(outputs: list[CliServerOutput], exit_code: int = 0) -> CliServerResponse:
    return CliServerResponse(exitCode=exit_code, outputs=outputs)


def build_error_response(message: str) -> CliServerResponse:
    return CliServerResponse(
        exitCode=1,
        outputs=[CliServerTextOutput(value=message, style="error")],
    )


def build_success_response(message: str) -> CliServerResponse:
    return CliServerResponse(
        exitCode=0,
        outputs=[CliServerTextOutput(value=message, style="success")],
    )


def format_as_json(data: Any) -> CliServerJsonOutput:
    return CliServerJsonOutput(value=data)


def format_as_table(headers: list[str], rows: list[list[str]]) -> CliServerTableOutput:
    return CliServerTableOutput(headers=headers, rows=rows)


def format_as_key_value(entries: dict[str, str]) -> CliServerKeyValueOutput:
    return CliServerKeyValueOutput(
        entries=[CliServerKeyValueEntry(key=k, value=v) for k, v in entries.items()]
    )


def format_as_list(items: list[str]) -> CliServerListOutput:
    return CliServerListOutput(items=items)


def get_output_format(command: CliProcessCommand) -> str:
    fmt = command.args.get("output")
    if fmt in ("json", "table", "text"):
        return fmt  # type: ignore[return-value]
    return "table"


def is_dry_run(command: CliProcessCommand) -> bool:
    return command.args.get("dry-run") is True or command.args.get("dryRun") is True


def apply_output_format(
    command: CliProcessCommand,
    default_output: CliServerOutput,
    raw_data: Any,
) -> CliServerOutput:
    fmt = get_output_format(command)
    if fmt == "json":
        return format_as_json(raw_data)
    if fmt == "text" and isinstance(default_output, CliServerTableOutput):
        text = "\n".join("\t".join(row) for row in default_output.rows)
        return CliServerTextOutput(value=text)
    return default_output
