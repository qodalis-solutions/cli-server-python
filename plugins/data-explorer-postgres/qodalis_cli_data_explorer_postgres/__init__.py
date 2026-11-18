"""Qodalis CLI Data Explorer PostgreSQL plugin."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🐘"

from .postgres_provider import PostgresDataExplorerProvider
__all__ = ["PostgresDataExplorerProvider"]
