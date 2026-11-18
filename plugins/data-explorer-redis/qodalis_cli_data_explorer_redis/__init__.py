"""Qodalis CLI Data Explorer Redis plugin — Redis provider."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🔴"

from .redis_provider import RedisDataExplorerProvider

__all__ = ["RedisDataExplorerProvider"]
