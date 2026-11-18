"""Qodalis CLI Data Explorer MSSQL plugin — MS SQL Server provider."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🗃️"

from .mssql_provider import MssqlDataExplorerProvider

__all__ = ["MssqlDataExplorerProvider"]
