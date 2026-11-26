"""The docs plugin's entry point (``qodalis_cli.plugins``): Markdown guides served at ``basePath`` (default
``/qcli/docs``), read from a folder (default ``<data dir>/plugin-data/docs/pages``, created) and the ``docs`` command.
The folder, path, title and name are settings (a restart applies them)."""

from __future__ import annotations

import os
from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .cli_docs_builder import DEFAULT_BASE_PATH, DEFAULT_NAME, DEFAULT_TITLE, CliDocsBuilder

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="docs", package="qodalis-cli-server-plugin-docs", title="Docs", icon="📖")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    text = {"type": "string", "requires_restart": True}
    return [
        CliConfigSettingDefinition(
            key="folder", label="Folder", description="The Markdown files (relative paths from the plugin's data directory)",
            default="pages", validation=CliConfigValidation(required=True, max=500), **text,
        ),
        CliConfigSettingDefinition(
            key="basePath", label="Path", description="Where the guides are served", default=DEFAULT_BASE_PATH,
            validation=CliConfigValidation(required=True, pattern=r"^(/[A-Za-z0-9._~-]+)+$"), **text,
        ),
        CliConfigSettingDefinition(key="title", label="Title", default=DEFAULT_TITLE, validation=CliConfigValidation(required=True, max=100), **text),
        CliConfigSettingDefinition(key="name", label="Name", default=DEFAULT_NAME, validation=CliConfigValidation(required=True, max=100), **text),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(title="Docs", description="Markdown guides served by the server", settings=settings())

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    folder = text("folder") or "pages"
    folder = folder if os.path.isabs(folder) else os.path.join(context.data_dir, folder)
    os.makedirs(folder, exist_ok=True)
    return (
        CliDocsBuilder()
        .set_base_path(text("basePath") or DEFAULT_BASE_PATH)
        .set_title(text("title") or DEFAULT_TITLE)
        .set_name(text("name") or DEFAULT_NAME)
        .add_folder(folder)
        .build()
    )
