from __future__ import annotations

import abc
from typing import Any

from ..models.cli_server_output import (
    CliServerKeyValueEntry,
    CliServerKeyValueOutput,
    CliServerJsonOutput,
    CliServerListOutput,
    CliServerOutput,
    CliServerTableOutput,
    CliServerTextOutput,
)
from ..models.cli_server_response import CliServerResponse


class ICliResponseBuilder(abc.ABC):
    @abc.abstractmethod
    def write_text(
        self,
        text: str,
        style: str | None = None,
    ) -> None: ...

    @abc.abstractmethod
    def write_table(self, headers: list[str], rows: list[list[str]]) -> None: ...

    @abc.abstractmethod
    def write_list(self, items: list[str], ordered: bool = False) -> None: ...

    @abc.abstractmethod
    def write_json(self, value: Any) -> None: ...

    @abc.abstractmethod
    def write_key_value(self, entries: dict[str, str]) -> None: ...

    @abc.abstractmethod
    def set_exit_code(self, code: int) -> None: ...

    @abc.abstractmethod
    def build(self) -> CliServerResponse: ...


class CliResponseBuilder(ICliResponseBuilder):
    def __init__(self) -> None:
        self._exit_code = 0
        self._outputs: list[CliServerOutput] = []

    def write_text(self, text: str, style: str | None = None) -> None:
        self._outputs.append(CliServerTextOutput(value=text, style=style))  # type: ignore[arg-type]

    def write_table(self, headers: list[str], rows: list[list[str]]) -> None:
        self._outputs.append(CliServerTableOutput(headers=headers, rows=rows))

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
