"""The SQLite file storage's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``): the filesystem API over a
file tree kept in a SQLite database (default ``<data dir>/plugin-data/filesystem-sqlite/files.db``).  Read-only only
when ``readOnly`` is turned on.  Refused when the host (or another plugin) already set the filesystem storage."""

from __future__ import annotations

import os
from typing import Any

from qodalis_cli_filesystem import read_only_file_storage
from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .sqlite_provider import SqliteFileStorageProvider, SqliteProviderOptions

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="filesystem-sqlite", package="qodalis-cli-server-plugin-filesystem-sqlite", title="Filesystem SQLite", icon="🗃️")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="dbPath", label="Database file",
                                   description="The SQLite file (relative paths from the plugin's data directory; :memory: for none)",
                                   type="string", default="files.db", validation=CliConfigValidation(required=True, max=500), **restart),
        CliConfigSettingDefinition(key="tablePrefix", label="Table prefix", description="Prepended to the table name (default Qcli)",
                                   type="string", default="Qcli", validation=CliConfigValidation(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"), **restart),
        CliConfigSettingDefinition(key="readOnly", label="Read-only", description="Uploads, new folders and deletes are refused",
                                   type="boolean", default=False, **restart),
    ]


async def create_plugin(context: Any) -> Any:
    if getattr(context.builder, "file_storage_provider", None) is not None:
        raise ValueError("the filesystem storage is already set by the host or another plugin")
    provider = await context.settings(title="Filesystem (SQLite)", description="A file tree kept in a SQLite database", settings=settings())

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    file = text("dbPath") or "files.db"
    storage = SqliteFileStorageProvider(SqliteProviderOptions(
        db_path=file if file == ":memory:" or os.path.isabs(file) else os.path.join(context.data_dir, file),
        table_prefix=text("tablePrefix") or None,
    ))
    context.builder.set_file_storage_provider(
        read_only_file_storage(storage) if provider.value("settings", "readOnly") is True else storage,
    )
    return None
