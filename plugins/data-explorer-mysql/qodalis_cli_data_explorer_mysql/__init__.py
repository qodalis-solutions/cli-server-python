"""Qodalis CLI Data Explorer MySQL plugin — MySQL provider."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🐬"

from .mysql_provider import MysqlDataExplorerProvider

__all__ = ["MysqlDataExplorerProvider"]
