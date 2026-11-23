"""Qodalis CLI filesystem-sqlite plugin — SQLite-based storage provider."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🪶"

from .sqlite_provider import SqliteFileStorageProvider, SqliteProviderOptions, filesystem_sqlite_migrations

__all__ = [
    "SqliteFileStorageProvider",
    "SqliteProviderOptions",
    "filesystem_sqlite_migrations",
]
