"""Qodalis CLI filesystem plugin — interfaces, models, errors, and providers."""

from __future__ import annotations

from .errors import (
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
)
from .i_file_storage_provider import IFileStorageProvider
from .models import FileEntry, FileStat

__all__ = [
    "FileEntry",
    "FileStat",
    "FileStorageExistsError",
    "FileStorageIsADirectoryError",
    "FileStorageNotADirectoryError",
    "FileStorageNotFoundError",
    "FileStoragePermissionError",
    "IFileStorageProvider",
]
