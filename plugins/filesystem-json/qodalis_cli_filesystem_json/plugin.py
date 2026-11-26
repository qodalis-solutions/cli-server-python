"""The JSON file storage's entry point (``qodalis_cli.plugins``): the filesystem API over a virtual tree kept in one
JSON file (default ``<data dir>/plugin-data/filesystem-json/files.json``).  Read-only unless ``readOnly`` is turned
off.  Refused when the host (or another plugin) already set the filesystem storage."""

from __future__ import annotations

import os
from typing import Any

from qodalis_cli_filesystem import read_only_file_storage
from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .json_file_provider import JsonFileProviderOptions, JsonFileStorageProvider

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(
    id="filesystem-json", package="qodalis-cli-server-plugin-filesystem-json", title="Filesystem JSON", icon="🗂️",
)


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return [
        CliConfigSettingDefinition(
            key="file", label="JSON file", description="The file the tree is kept in (relative paths from the plugin's data directory)",
            type="string", default="files.json", requires_restart=True, validation=CliConfigValidation(required=True, max=500),
        ),
        CliConfigSettingDefinition(key="readOnly", label="Read-only", description="Uploads, new folders and deletes are refused",
                                   type="boolean", default=True, requires_restart=True),
    ]


async def create_plugin(context: Any) -> Any:
    if getattr(context.builder, "file_storage_provider", None) is not None:
        raise ValueError("the filesystem storage is already set by the host or another plugin")
    provider = await context.settings(
        title="Filesystem (JSON file)", description="A virtual file tree kept in one JSON file", settings=settings(),
    )
    file = str(provider.value("settings", "file") or "files.json")
    storage = JsonFileStorageProvider(JsonFileProviderOptions(file_path=file if os.path.isabs(file) else os.path.join(context.data_dir, file)))
    context.builder.set_file_storage_provider(
        storage if provider.value("settings", "readOnly") is False else read_only_file_storage(storage),
    )
    return None
