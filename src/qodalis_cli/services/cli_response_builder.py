from __future__ import annotations

import abc
from typing import Any

from ..abstractions import CliPageRequest
from ..models.cli_server_output import (
    CliServerKeyValueEntry,
    CliServerKeyValueOutput,
    CliServerJsonOutput,
    CliServerListOutput,
    CliServerOutput,
    CliServerTableOutput,
    CliServerTablePage,
    CliServerTextOutput,
)
from ..models.cli_server_response import CliServerResponse


class ICliResponseBuilder(abc.ABC):
    """Interface for incrementally building a ``CliServerResponse``."""

    @abc.abstractmethod
    def write_text(
        self,
        text: str,
        style: str | None = None,
    ) -> None:
        """Append a text output."""
        ...

    @abc.abstractmethod
    def write_table(self, headers: list[str], rows: list[list[str]]) -> None:
        """Append a table output."""
        ...

    @abc.abstractmethod
    def write_paged_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        page: CliPageRequest,
        total: int | None = None,
        has_more: bool | None = None,
    ) -> None:
        """Append one page of a longer table.  The terminal shows which rows
        these are under it and the command for the next ones.

        Args:
            headers: Column headers.
            rows: The rows of this page.
            page: The rows that were asked for (``CliPaging.from_command``).
            total: All rows across pages, when known.
            has_more: Whether more rows follow; defaults to
                ``page.skip + len(rows) < total`` (query ``take + 1`` rows to
                find out when the total is unknown).
        """
        ...

    @abc.abstractmethod
    def write_list(self, items: list[str], ordered: bool = False) -> None:
        """Append a list output."""
        ...

    @abc.abstractmethod
    def write_json(self, value: Any) -> None:
        """Append a JSON output."""
        ...

    @abc.abstractmethod
    def write_key_value(self, entries: dict[str, str]) -> None:
        """Append a key-value output."""
        ...

    @abc.abstractmethod
    def set_exit_code(self, code: int) -> None:
        """Set the response exit code."""
        ...

    @abc.abstractmethod
    def build(self) -> CliServerResponse:
        """Build and return the final ``CliServerResponse``."""
        ...


class CliResponseBuilder(ICliResponseBuilder):
    """Default builder that accumulates outputs and produces a ``CliServerResponse``."""

    def __init__(self) -> None:
        self._exit_code = 0
        self._outputs: list[CliServerOutput] = []

    def write_text(self, text: str, style: str | None = None) -> None:
        self._outputs.append(CliServerTextOutput(value=text, style=style))  # type: ignore[arg-type]

    def write_table(self, headers: list[str], rows: list[list[str]]) -> None:
        self._outputs.append(CliServerTableOutput(headers=headers, rows=rows))

    def write_paged_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        page: CliPageRequest,
        total: int | None = None,
        has_more: bool | None = None,
    ) -> None:
        if has_more is None:
            has_more = total is not None and page.skip + len(rows) < total
        self._outputs.append(
            CliServerTableOutput(
                headers=headers,
                rows=rows,
                page=CliServerTablePage(
                    skip=page.skip,
                    take=page.take,
                    total=total,
                    has_more=has_more,
                ),
            )
        )

    def write_list(self, items: list[str], ordered: bool = False) -> None:
        self._outputs.append(CliServerListOutput(items=items, ordered=ordered or None))

    def write_json(self, value: Any) -> None:
        self._outputs.append(CliServerJsonOutput(value=value))

    def write_key_value(self, entries: dict[str, str]) -> None:
        self._outputs.append(
            CliServerKeyValueOutput(
                entries=[CliServerKeyValueEntry(key=k, value=v) for k, v in entries.items()]
            )
        )

    def set_exit_code(self, code: int) -> None:
        self._exit_code = code

    def build(self) -> CliServerResponse:
        return CliServerResponse(exitCode=self._exit_code, outputs=self._outputs)
