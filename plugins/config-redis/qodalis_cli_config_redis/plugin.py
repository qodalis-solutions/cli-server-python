"""The config-redis plugin's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``): Redis becomes the server's
default config store (``CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE``): the settings of the providers registered before it
move to Redis, those registered after it start there.  Its own settings (the URL a secret) are kept in
``<data dir>/plugin-settings.json``, never in Redis itself.  Without a URL (or a ``client`` in code) it needs
configuration and the settings stay where they were."""

from __future__ import annotations

import os
from typing import Any

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation, JsonFileConfigStore

from .redis_config_store import DEFAULT_REDIS_CONFIG_KEY_PREFIX, CliRedisConfigStore

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="config-redis", package="qodalis-cli-server-plugin-config-redis", title="Config on Redis", icon="🟥")

#: Before the providers resolve their store.
order = CLI_PLUGIN_LOAD_ORDER.CONFIG_STORE


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults; ``client``, a redis.asyncio client, from code)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="url", label="Redis URL",
                                   description="redis://[:password@]host:6379/0 (rediss:// for TLS); kept as a secret, never returned",
                                   type="secret", required=True, **restart),
        CliConfigSettingDefinition(key="keyPrefix", label="Key prefix", description="Prepended to every key: <prefix><provider>:<section>",
                                   type="string", default=DEFAULT_REDIS_CONFIG_KEY_PREFIX, validation=CliConfigValidation(max=200), **restart),
        CliConfigSettingDefinition(key="ttlSeconds", label="Expiry (seconds)", description="A section expires this long after its last change; 0: never",
                                   type="number", default=0, validation=CliConfigValidation(min=0), **restart),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(
        title="Config on Redis", description="Redis as the server's config store", settings=settings(),
        # Not in Redis: the URL must be read before Redis can be
        store=context.settings_file or JsonFileConfigStore(os.path.join(context.server_data_dir, "plugin-settings.json")),
    )
    client = context.options.get("client")
    url = str(provider.value("settings", "url") or "").strip()
    if client is None and not url:
        return context.needs_configuration(provider.missing_required())
    ttl = provider.value("settings", "ttlSeconds")
    store = CliRedisConfigStore(
        client=client, url=None if client is not None else url,
        key_prefix=str(provider.value("settings", "keyPrefix") or DEFAULT_REDIS_CONFIG_KEY_PREFIX),
        ttl_seconds=int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else None,
    )
    context.builder.replace_config_store(store)
    if context.restart is not None and hasattr(context.restart, "on_shutdown"):
        context.restart.on_shutdown("config-redis store", store.close)
    return None
