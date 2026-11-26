"""The data explorer's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``).  The data explorer itself is part
of the server (its API, the dashboard's page and the ``data_*`` MCP tools are served once a source is added); this
entry sets the rules every source added after it follows (``CLI_PLUGIN_LOAD_ORDER.CORE``, before the sources at
``SOURCE``): read-only for all, a row cap, a rate limit for sources without their own.  Shared as the
``data-explorer.policy`` service, which the server's ``add_data_explorer_provider`` applies.

The package comes with the server when the data explorer is used; from the marketplace it is enabled without being
downloaded when the server's environment has it."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation
from qodalis_cli_server_abstractions.data_explorer_types import DataExplorerRateLimit

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="data-explorer", package="qodalis-cli-server-plugin-data-explorer", title="Data explorer", icon="🔍")

#: Before its sources.
order = CLI_PLUGIN_LOAD_ORDER.CORE

#: The service the rules are shared as (read by the server when a source is added).
DATA_EXPLORER_POLICY_SERVICE = "data-explorer.policy"


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="forceReadOnly", label="Read-only for all", description="Every source may only read, whatever its own setting",
                                   type="boolean", default=False, **restart),
        CliConfigSettingDefinition(key="maxRows", label="Rows at most", description="The most rows any source returns; 0: each source its own",
                                   type="number", default=0, validation=CliConfigValidation(min=0, max=1_000_000), **restart),
        CliConfigSettingDefinition(key="rateLimitQueries", label="Queries per window",
                                   description="Per caller and source, for sources without their own limit; 0: none",
                                   type="number", default=0, validation=CliConfigValidation(min=0), **restart),
        CliConfigSettingDefinition(key="rateLimitWindowSeconds", label="Window (seconds)", type="number", default=60,
                                   validation=CliConfigValidation(min=1, max=86400), **restart),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="Data explorer", description="Rules every data explorer source follows", settings=settings())

    def num(key: str) -> int:
        value = provider.value("settings", key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    queries = num("rateLimitQueries")
    context.services.set(DATA_EXPLORER_POLICY_SERVICE, {
        "force_read_only": provider.value("settings", "forceReadOnly") is True,
        "max_rows": num("maxRows"),
        "rate_limit": DataExplorerRateLimit(queries, max(1, num("rateLimitWindowSeconds") or 60)) if queries > 0 else None,
    })
    return None
