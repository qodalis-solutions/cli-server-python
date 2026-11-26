"""Qodalis CLI filesystem plugin — interfaces, models, errors, and providers."""

from __future__ import annotations

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "📁"

from .errors import (
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
)
from .i_file_storage_provider import IFileStorageProvider
from .models import FileEntry, FileStat
from .read_only import ReadOnlyFileStorage, read_only_file_storage

__all__ = [
    "FileEntry",
    "FileStat",
    "FileStorageExistsError",
    "FileStorageIsADirectoryError",
    "FileStorageNotADirectoryError",
    "FileStorageNotFoundError",
    "FileStoragePermissionError",
    "IFileStorageProvider",
    "ReadOnlyFileStorage",
    "read_only_file_storage",
]
