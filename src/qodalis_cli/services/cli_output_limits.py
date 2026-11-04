"""Caps unpaged tables at ``CliServerOptions.max_table_rows``."""

from __future__ import annotations

from typing import Any, TypeVar

from ..models.cli_server_output import CliServerTableOutput, CliServerTablePage
from ..models.cli_server_response import CliServerResponse

#: The most rows a table may return without paging, by default.
DEFAULT_MAX_TABLE_ROWS = 200

_Output = TypeVar("_Output")


def apply_output_limits(output: _Output, max_rows: int) -> _Output:
    """``output``, or for a table without a page that has more than
    ``max_rows`` rows, a copy with the first rows and a page marked truncated.

    Accepts an output model or its serialised ``dict`` form (what stream
    processors emit); the result has the same form.
    """
    if max_rows <= 0:
        return output
    if isinstance(output, CliServerTableOutput):
        if output.page is not None or len(output.rows) <= max_rows:
            return output
        return CliServerTableOutput(  # type: ignore[return-value]
            headers=output.headers,
            rows=output.rows[:max_rows],
            page=_truncated_page(max_rows, len(output.rows)),
        )
    if isinstance(output, dict) and output.get("type") == "table" and output.get("page") is None:
        rows: list[Any] = output.get("rows") or []
        if len(rows) <= max_rows:
            return output
        page = _truncated_page(max_rows, len(rows)).model_dump(by_alias=True, exclude_none=True)
        return {**output, "rows": rows[:max_rows], "page": page}  # type: ignore[return-value]
    return output


def apply_response_limits(response: CliServerResponse, max_rows: int) -> CliServerResponse:
    """``response`` with :func:`apply_output_limits` applied to every output."""
    if max_rows <= 0:
        return response
    response.outputs = [apply_output_limits(o, max_rows) for o in response.outputs]
    return response


def _truncated_page(max_rows: int, total: int) -> CliServerTablePage:
    return CliServerTablePage(skip=0, take=max_rows, total=total, has_more=True, truncated=True)
