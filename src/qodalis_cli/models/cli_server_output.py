from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CliServerTextOutput(BaseModel):
    """Plain text output with an optional style hint."""

    type: Literal["text"] = "text"
    value: str
    style: Literal["success", "error", "info", "warning"] | None = None


class CliServerTablePage(BaseModel):
    """The page of a table output:
    ``{"skip": 50, "take": 50, "total": 1987, "hasMore": true}``."""

    skip: int
    """Rows skipped before the first one shown."""
    take: int
    """Rows asked for."""
    total: int | None = None
    """All rows across pages, when the command knows it."""
    has_more: bool = Field(alias="hasMore")
    """Whether more rows follow."""
    truncated: bool | None = None
    """Set when the server cut an unpaged table (``CliServerOptions.max_table_rows``):
    the command does not page, so ``--skip`` will not show the rest."""

    model_config = {"populate_by_name": True}


class CliServerTableOutput(BaseModel):
    """Tabular output with headers and rows."""

    type: Literal["table"] = "table"
    headers: list[str]
    rows: list[list[str]]
    page: CliServerTablePage | None = None
    """Which part of a longer result the rows are, when the command pages
    (``--skip``/``--take``); the terminal shows it under the table with the
    command for the next rows."""


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
