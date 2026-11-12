"""Qodalis CLI Data Explorer plugin — registry, executor, and controller."""

from .data_explorer_registry import DataExplorerRegistry
from .data_explorer_executor import (
    AUDIT_QUERY_MAX_LENGTH,
    DataExplorerAuditSink,
    DataExplorerExecutor,
    result_to_dict,
)
from .data_explorer_controller import create_data_explorer_router
from .data_explorer_policy import (
    MASK,
    DataExplorerRateLimiter,
    apply_column_policy,
    can_export,
    can_use_source,
    filter_listing,
    filter_schema,
    glob_to_regex,
    is_object_allowed,
    is_read_only,
    masked_column_error,
    masked_result_error,
    matches_any,
    missing_permissions,
    objects_error,
    query_objects,
    read_only_error,
    sql_objects,
)

__all__ = [
    "AUDIT_QUERY_MAX_LENGTH",
    "DataExplorerAuditSink",
    "DataExplorerExecutor",
    "DataExplorerRateLimiter",
    "DataExplorerRegistry",
    "MASK",
    "apply_column_policy",
    "can_export",
    "can_use_source",
    "create_data_explorer_router",
    "filter_listing",
    "filter_schema",
    "glob_to_regex",
    "is_object_allowed",
    "is_read_only",
    "masked_column_error",
    "masked_result_error",
    "matches_any",
    "missing_permissions",
    "objects_error",
    "query_objects",
    "read_only_error",
    "result_to_dict",
    "sql_objects",
]
