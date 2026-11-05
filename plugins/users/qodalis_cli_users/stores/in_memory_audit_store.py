"""The in-memory audit store moved to the audit plugin
(``qodalis_cli_audit``); re-exported here for earlier imports."""

from qodalis_cli_audit.in_memory_audit_store import (
    FAILED_LOGIN_EVENT,
    MAX_ENTRIES,
    MAX_FAILED_LOGIN_ENTRIES,
    InMemoryAuditStore,
)

__all__ = [
    "FAILED_LOGIN_EVENT",
    "MAX_ENTRIES",
    "MAX_FAILED_LOGIN_ENTRIES",
    "InMemoryAuditStore",
]
