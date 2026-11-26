"""The filesystem plugin's entry point (``qodalis_cli.plugins``): the filesystem API, the ``fs_*`` MCP tools and the
dashboard's Files page over folders of the server's disk.  The folders are a required setting (none by default: the
plugin needs configuration until some are set, then a restart), and the storage is read-only unless ``readOnly`` is
turned off.  Refused when the host (or another plugin) already set the filesystem storage.

The package comes with the server (the server depends on it), so the marketplace enables it without downloading it."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .providers.os_provider import OsFileStorageProvider, OsProviderOptions
from .read_only import read_only_file_storage

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="filesystem", package="qodalis-cli-server-plugin-filesystem", title="Filesystem", icon="📁")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    return [
        CliConfigSettingDefinition(
            key="roots", label="Folders",
            description="Absolute paths served (and everything below them); none: the filesystem API serves nothing",
            type="string[]", required=True, default=[], requires_restart=True,
            validation=CliConfigValidation(pattern=r"^(/|[A-Za-z]:[\\/]).*"),
        ),
        CliConfigSettingDefinition(
            key="readOnly", label="Read-only",
            description="Only list, read and download: uploads, new folders and deletes are refused",
            type="boolean", default=True, requires_restart=True,
        ),
    ]


async def create_plugin(context: Any) -> Any:
    if getattr(context.builder, "file_storage_provider", None) is not None:
        raise ValueError("the filesystem storage is already set by the host or another plugin")
    # Not ``filesystem``: the server's own provider of that id describes the storage in use
    provider = await context.settings(
        id="filesystem-os", title="Filesystem (server disk)",
        description="Folders of the server's disk served by the filesystem API", settings=settings(),
    )
    roots = [r for r in provider.value("settings", "roots") or [] if isinstance(r, str) and r.strip()]
    if not roots:
        return context.needs_configuration(provider.missing_required() or ["settings.roots"])
    storage = OsFileStorageProvider(OsProviderOptions(allowed_paths=roots))
    context.builder.set_file_storage_provider(
        storage if provider.value("settings", "readOnly") is False else read_only_file_storage(storage),
    )
    return None
