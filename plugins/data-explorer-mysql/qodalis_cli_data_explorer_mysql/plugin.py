"""The MySQL data explorer source's entry point (``qodalis_cli.plugins``): one source of the data explorer
(``/api/qcli/data-explorer``, the dashboard's Data explorer page, the ``data_*`` MCP tools), its connection as settings
(a ``secret``: kept server-side, never returned).  Read-only by default.  Nothing is added until it is configured
(then a restart): until then the plugin needs configuration."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition
from qodalis_cli_server_abstractions.data_explorer_source_entry import (
    DataExplorerSourceEntry,
    connection_setting,
    create_data_explorer_source,
    data_explorer_source_settings,
)
from qodalis_cli_server_abstractions.data_explorer_types import DataExplorerLanguage, DataExplorerOutputFormat

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="data-explorer-mysql", package="qodalis-cli-server-plugin-data-explorer-mysql", title="Data explorer: MySQL", icon="🐬")

#: Loads after the data explorer's own entry.
order = CLI_PLUGIN_LOAD_ORDER.SOURCE


def _create(v: dict[str, Any], context: Any) -> Any:
    from .mysql_provider import MysqlDataExplorerProvider

    return MysqlDataExplorerProvider(str(v["connectionString"]))


ENTRY = DataExplorerSourceEntry(
    title="Data explorer: MySQL",
    name="mysql",
    description="MySQL database",
    language=DataExplorerLanguage.SQL,
    output=DataExplorerOutputFormat.TABLE,
    connection=[
        connection_setting("e.g. mysql://user:password@host:3306/database"),
    ],
    create=_create,
)


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return data_explorer_source_settings(ENTRY)


async def create_plugin(context: Any) -> Any:
    return await create_data_explorer_source(context, ENTRY)
