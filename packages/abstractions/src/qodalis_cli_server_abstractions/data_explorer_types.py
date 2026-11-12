"""Data Explorer type definitions for the Qodalis CLI server."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal


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
class DataExplorerRateLimit:
    """Rate limit of a data source: at most ``max_queries`` executes per
    caller per ``window_seconds``."""

    max_queries: int
    window_seconds: float


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
    #: Permissions the caller must hold (all of them, besides ``data:query``)
    #: to see and use the source.  Ignored on an open server.
    required_permissions: list[str] | None = None
    #: Queries may only read (default ``True``): the server checks the query
    #: by language and SQL providers run it in a read-only transaction.  Set
    #: ``False`` for a source that writes.
    read_only: bool = True
    #: Glob patterns (``*``, case-insensitive) of the tables (``schema.table``
    #: or ``table``), collections, Redis keys or Elasticsearch indices the
    #: source shows and accepts.  Best effort: the database account's
    #: permissions are the real boundary.
    allowed_objects: list[str] | None = None
    #: Glob patterns of columns/fields whose values are replaced by ``***``.
    mask_columns: list[str] | None = None
    #: Glob patterns of columns/fields removed from results and the schema.
    drop_columns: list[str] | None = None
    #: Write every execute to the audit log when the server has one.
    audit: bool = True
    #: At most ``max_queries`` executes per caller per ``window_seconds``.
    rate_limit: DataExplorerRateLimit | None = None
    #: Permissions needed to export results (``json``/``csv`` output in the
    #: web CLI).  Advisory.
    export_permissions: list[str] | None = None


#: Why the server refused a data explorer query.
DataExplorerErrorCode = Literal[
    "forbidden", "rate_limited", "read_only", "object_not_allowed", "masked_column", "unknown_source",
]


def is_read_only_source(options: Any) -> bool:
    """Whether a source only reads (``read_only`` defaults to ``True``)."""
    return getattr(options, "read_only", True) is not False


@dataclass
class DataExplorerCaller:
    """Who runs a data explorer request, as the server knows it.

    ``has_permission`` is ``None`` on an open server (no auth provider):
    permission options are then not enforced."""

    user_id: str | None = None
    user_name: str | None = None
    #: The client address, for rate limiting anonymous callers.
    remote_address: str | None = None
    has_permission: Callable[[str], bool] | None = None


@dataclass
class DataExplorerAuditEntry:
    """An audit entry the data explorer writes (event ``data_query``)."""

    event: str
    user_id: str
    user_name: str
    metadata: dict[str, Any]


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
    #: Why the server refused the query, when it did: ``forbidden`` (missing
    #: permission, 403), ``rate_limited`` (429), ``read_only``,
    #: ``object_not_allowed``, ``masked_column``, ``unknown_source``.
    error_code: DataExplorerErrorCode | None = None


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
