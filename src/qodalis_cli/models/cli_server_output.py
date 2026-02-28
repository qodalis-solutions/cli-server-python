from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class CliServerTextOutput(BaseModel):
    type: Literal["text"] = "text"
    value: str
    style: Literal["success", "error", "info", "warning"] | None = None


class CliServerTableOutput(BaseModel):
    type: Literal["table"] = "table"
    headers: list[str]
    rows: list[list[str]]


class CliServerListOutput(BaseModel):
    type: Literal["list"] = "list"
    items: list[str]
    ordered: bool | None = None


class CliServerJsonOutput(BaseModel):
    type: Literal["json"] = "json"
    value: Any


class CliServerKeyValueEntry(BaseModel):
    key: str
    value: str


class CliServerKeyValueOutput(BaseModel):
    type: Literal["key-value"] = "key-value"
    entries: list[CliServerKeyValueEntry]


CliServerOutput = (
    CliServerTextOutput
    | CliServerTableOutput
    | CliServerListOutput
    | CliServerJsonOutput
    | CliServerKeyValueOutput
)
