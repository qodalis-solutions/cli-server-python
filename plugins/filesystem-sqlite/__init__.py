"""Qodalis CLI filesystem-sqlite plugin — SQLite-based storage provider.

Re-exports from ``qodalis_cli_filesystem_sqlite`` for backward compatibility.
"""

from __future__ import annotations

from qodalis_cli_filesystem_sqlite import SqliteFileStorageProvider, SqliteProviderOptions

__all__ = [
    "SqliteFileStorageProvider",
    "SqliteProviderOptions",
]
