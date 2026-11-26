"""The SQLite data explorer source's entry point (``qodalis_cli.plugins``): one source of the data explorer
(``/api/qcli/data-explorer``, the dashboard's Data explorer page, the ``data_*`` MCP tools) over a SQLite file (default
``<data dir>/plugin-data/data-explorer-sql/data.db``).  Read-only by default."""

from __future__ import annotations

import os
from typing import Any

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation
from qodalis_cli_server_abstractions.data_explorer_source_entry import (
    DataExplorerSourceEntry,
    create_data_explorer_source,
    data_explorer_source_settings,
)
from qodalis_cli_server_abstractions.data_explorer_types import DataExplorerLanguage, DataExplorerOutputFormat

from .sql_provider import SqlDataExplorerProvider

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="data-explorer-sql", package="qodalis-cli-server-plugin-data-explorer-sql", title="Data explorer: SQLite", icon="🧮")

#: Loads after the data explorer's own entry.
order = CLI_PLUGIN_LOAD_ORDER.SOURCE


def _create(v: dict[str, Any], context: Any) -> Any:
    filename = str(v.get("filename") or "data.db").strip() or "data.db"
    if filename != ":memory:" and not os.path.isabs(filename):
        filename = os.path.join(context.data_dir, filename)
    return SqlDataExplorerProvider(filename)


ENTRY = DataExplorerSourceEntry(
    title="Data explorer: SQLite",
    name="sql",
    description="SQLite database",
    language=DataExplorerLanguage.SQL,
    output=DataExplorerOutputFormat.TABLE,
    connection=[
        CliConfigSettingDefinition(
            key="filename", label="SQLite file",
            description="The database file (relative paths from the plugin's data directory; :memory: for none)",
            type="string", default="data.db", requires_restart=True, validation=CliConfigValidation(required=True, max=500),
        ),
    ],
    create=_create,
)


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return data_explorer_source_settings(ENTRY)


async def create_plugin(context: Any) -> Any:
    return await create_data_explorer_source(context, ENTRY)
