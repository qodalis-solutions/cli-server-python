from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class CliServerTextOutput(BaseModel):
    """Plain text output with an optional style hint."""

    type: Literal["text"] = "text"
    value: str
    style: Literal["success", "error", "info", "warning"] | None = None


class CliServerTableOutput(BaseModel):
    """Tabular output with headers and rows."""

    type: Literal["table"] = "table"
    headers: list[str]
    rows: list[list[str]]


class CliServerListOutput(BaseModel):
    """List output, optionally ordered."""

    type: Literal["list"] = "list"
    items: list[str]
    ordered: bool | None = None


class CliServerJsonOutput(BaseModel):
    """Arbitrary JSON value output."""

    type: Literal["json"] = "json"
    value: Any


class CliServerKeyValueEntry(BaseModel):
    """A single key-value pair."""

    key: str
    value: str


class CliServerKeyValueOutput(BaseModel):
    """Key-value pair list output."""

    type: Literal["key-value"] = "key-value"
    entries: list[CliServerKeyValueEntry]


CliServerOutput = (
    CliServerTextOutput
    | CliServerTableOutput
    | CliServerListOutput
    | CliServerJsonOutput
    | CliServerKeyValueOutput
)
"""Union type representing all supported server output formats."""
