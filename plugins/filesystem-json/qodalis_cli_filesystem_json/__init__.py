"""Qodalis CLI filesystem-json plugin — JSON file-based storage provider."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "📄"

from .json_file_provider import JsonFileProviderOptions, JsonFileStorageProvider

__all__ = [
    "JsonFileProviderOptions",
    "JsonFileStorageProvider",
]
