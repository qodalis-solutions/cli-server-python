"""Qodalis CLI config-redis plugin: writable config settings kept in Redis."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🔴"

from .redis_config_store import (
    DEFAULT_REDIS_CONFIG_KEY_PREFIX,
    REDIS_CONFIG_WRITE_SCRIPT,
    CliRedisConfigStore,
)

__all__ = ["CliRedisConfigStore", "DEFAULT_REDIS_CONFIG_KEY_PREFIX", "REDIS_CONFIG_WRITE_SCRIPT"]
