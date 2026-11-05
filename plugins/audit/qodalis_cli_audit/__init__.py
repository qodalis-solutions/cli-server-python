"""Qodalis CLI audit plugin — the audit log (who did what, to whom or what,
and when), its in-memory store and its API at ``/api/v1/qcli/audit``."""

from __future__ import annotations

from qodalis_cli_server_abstractions.audit import (
    AuditEntry,
    AuditMetadata,
    AuditQueryOptions,
    IAuditStore,
)

from .cli_audit_builder import PREFIX, CliAuditBuilder, CliAuditPlugin, get_audit_plugin
from .cli_audit_controller import (
    AUDIT_CLEAR,
    AUDIT_CLEARED_EVENT,
    AUDIT_READ,
    DEFAULT_AUDIT_LIMIT,
    MAX_AUDIT_LIMIT,
    create_audit_router,
)
from .in_memory_audit_store import (
    FAILED_LOGIN_EVENT,
    MAX_ENTRIES,
    MAX_FAILED_LOGIN_ENTRIES,
    InMemoryAuditStore,
)

__all__ = [
    "AUDIT_CLEAR",
    "AUDIT_CLEARED_EVENT",
    "AUDIT_READ",
    "AuditEntry",
    "AuditMetadata",
    "AuditQueryOptions",
    "CliAuditBuilder",
    "CliAuditPlugin",
    "DEFAULT_AUDIT_LIMIT",
    "FAILED_LOGIN_EVENT",
    "IAuditStore",
    "InMemoryAuditStore",
    "MAX_AUDIT_LIMIT",
    "MAX_ENTRIES",
    "MAX_FAILED_LOGIN_ENTRIES",
    "PREFIX",
    "create_audit_router",
    "get_audit_plugin",
]
