"""Qodalis CLI Data Explorer MongoDB plugin — MongoDB provider."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🍃"

from .mongo_provider import MongoDataExplorerProvider

__all__ = ["MongoDataExplorerProvider"]
