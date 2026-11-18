"""Qodalis CLI Data Explorer SQL plugin — SQLite provider."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🪶"

from .sql_provider import SqlDataExplorerProvider

__all__ = ["SqlDataExplorerProvider"]
