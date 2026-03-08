"""Qodalis CLI filesystem-json plugin — JSON file-based storage provider.

Re-exports from ``qodalis_cli_filesystem_json`` for backward compatibility.
"""

from __future__ import annotations

from qodalis_cli_filesystem_json import JsonFileProviderOptions, JsonFileStorageProvider

__all__ = [
    "JsonFileProviderOptions",
    "JsonFileStorageProvider",
]
