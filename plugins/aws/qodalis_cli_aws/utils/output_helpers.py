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
    """Wraps a list of output objects into a CLI server response.

    Args:
        outputs: Output objects to include in the response.
        exit_code: Process exit code (0 for success).

    Returns:
        A ``CliServerResponse`` containing the provided outputs.
    """
    return CliServerResponse(exitCode=exit_code, outputs=outputs)


def build_error_response(message: str) -> CliServerResponse:
    """Creates an error response with exit code 1.

    Args:
        message: Error message to display.

    Returns:
        A ``CliServerResponse`` with error styling.
    """
    return CliServerResponse(
        exitCode=1,
        outputs=[CliServerTextOutput(value=message, style="error")],
    )


def build_success_response(message: str) -> CliServerResponse:
    """Creates a success response with exit code 0.

    Args:
        message: Success message to display.

    Returns:
        A ``CliServerResponse`` with success styling.
    """
    return CliServerResponse(
        exitCode=0,
        outputs=[CliServerTextOutput(value=message, style="success")],
    )


def format_as_json(data: Any) -> CliServerJsonOutput:
    """Wraps arbitrary data as a JSON output object.

    Args:
        data: Data to serialize as JSON.

    Returns:
        A ``CliServerJsonOutput`` wrapping the data.
    """
    return CliServerJsonOutput(value=data)


def format_as_table(headers: list[str], rows: list[list[str]]) -> CliServerTableOutput:
    """Creates a table output with the given headers and rows.

    Args:
        headers: Column header labels.
        rows: Row data as lists of strings.

    Returns:
        A ``CliServerTableOutput`` for tabular display.
    """
    return CliServerTableOutput(headers=headers, rows=rows)


def format_as_key_value(entries: dict[str, str]) -> CliServerKeyValueOutput:
    """Creates a key-value output from a dictionary.

    Args:
        entries: Key-value pairs to display.

    Returns:
        A ``CliServerKeyValueOutput`` containing the entries.
    """
    return CliServerKeyValueOutput(
        entries=[CliServerKeyValueEntry(key=k, value=v) for k, v in entries.items()]
    )


def format_as_list(items: list[str]) -> CliServerListOutput:
    """Creates a list output from string items.

    Args:
        items: Items to display as a list.

    Returns:
        A ``CliServerListOutput`` containing the items.
    """
    return CliServerListOutput(items=items)


def get_output_format(command: CliProcessCommand) -> str:
    """Extracts the requested output format from command arguments.

    Args:
        command: The CLI command being processed.

    Returns:
        One of ``"json"``, ``"table"``, or ``"text"`` (defaults to ``"table"``).
    """
    fmt = command.args.get("output")
    if fmt in ("json", "table", "text"):
        return fmt  # type: ignore[return-value]
    return "table"


def is_dry_run(command: CliProcessCommand) -> bool:
    """Checks whether the command was invoked with the dry-run flag.

    Args:
        command: The CLI command being processed.

    Returns:
        ``True`` if dry-run mode is enabled.
    """
    return command.args.get("dry-run") is True or command.args.get("dryRun") is True


def apply_output_format(
    command: CliProcessCommand,
    default_output: CliServerOutput,
    raw_data: Any,
) -> CliServerOutput:
    """Converts the default output to the format requested by ``--output``.

    If the user requested JSON, returns a JSON output. If text was requested
    and the default is a table, converts rows to tab-separated text.

    Args:
        command: The CLI command containing output format arguments.
        default_output: The pre-built output to use when no override applies.
        raw_data: Raw data for JSON serialization when JSON format is requested.

    Returns:
        The output object in the requested format.
    """
    fmt = get_output_format(command)
    if fmt == "json":
        return format_as_json(raw_data)
    if fmt == "text" and isinstance(default_output, CliServerTableOutput):
        text = "\n".join("\t".join(row) for row in default_output.rows)
        return CliServerTextOutput(value=text)
    return default_output
