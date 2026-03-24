"""Data Explorer type definitions for the Qodalis CLI server."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DataExplorerLanguage(str, Enum):
    """Supported query languages for data explorer providers."""

    SQL = "sql"
    JSON = "json"
    SHELL = "shell"
    GRAPHQL = "graphql"
    REDIS = "redis"
    ELASTICSEARCH = "elasticsearch"


class DataExplorerOutputFormat(str, Enum):
    """Output formats for data explorer query results."""

    TABLE = "table"
    JSON = "json"
    CSV = "csv"
    RAW = "raw"


@dataclass
class DataExplorerTemplate:
    """A predefined query template with optional parameters."""

    name: str
    query: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


@dataclass
class DataExplorerParameterDescriptor:
    """Describes a parameter accepted by a data explorer provider."""

    name: str
    description: str | None = None
    required: bool = False
    default_value: Any = None


@dataclass
class DataExplorerProviderOptions:
    """Configuration options for a data explorer provider."""

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
    """Context passed to a data explorer provider when executing a query."""

    query: str
    parameters: dict[str, Any]
    options: DataExplorerProviderOptions


@dataclass
class DataExplorerResult:
    """Result returned from a data explorer query execution."""

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


@dataclass
class DataExplorerSchemaColumn:
    """Describes a single column in a data explorer schema."""

    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


@dataclass
class DataExplorerSchemaTable:
    """Describes a table or collection in a data explorer schema."""

    name: str
    type: str
    columns: list[DataExplorerSchemaColumn] = field(default_factory=list)


@dataclass
class DataExplorerSchemaResult:
    """Schema introspection result from a data explorer provider."""

    source: str
    tables: list[DataExplorerSchemaTable] = field(default_factory=list)
