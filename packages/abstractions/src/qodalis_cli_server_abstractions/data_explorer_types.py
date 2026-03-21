"""Data Explorer type definitions for the Qodalis CLI server."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DataExplorerLanguage(str, Enum):
    SQL = "sql"
    JSON = "json"
    SHELL = "shell"
    GRAPHQL = "graphql"


class DataExplorerOutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"
    RAW = "raw"


@dataclass
class DataExplorerTemplate:
    name: str
    query: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


@dataclass
class DataExplorerParameterDescriptor:
    name: str
    description: str | None = None
    required: bool = False
    default_value: Any = None


@dataclass
class DataExplorerProviderOptions:
    name: str
    description: str
    language: DataExplorerLanguage = DataExplorerLanguage.SQL
    default_output_format: DataExplorerOutputFormat = DataExplorerOutputFormat.TABLE
    parameters: list[DataExplorerParameterDescriptor] = field(default_factory=list)
    templates: list[DataExplorerTemplate] = field(default_factory=list)
    timeout: int = 30000
    max_rows: int = 1000


@dataclass
class DataExplorerExecutionContext:
    query: str
    parameters: dict[str, Any]
    options: DataExplorerProviderOptions


@dataclass
class DataExplorerResult:
    success: bool
    source: str
    language: DataExplorerLanguage
    default_output_format: DataExplorerOutputFormat
    execution_time: int
    columns: list[str] | None
    rows: list[list[Any]] | list[dict[str, Any]]
    row_count: int
    truncated: bool
    error: str | None
