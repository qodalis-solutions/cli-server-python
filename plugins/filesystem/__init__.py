"""Qodalis CLI filesystem plugin — interfaces, models, errors, and providers.

This module re-exports everything from the ``qodalis_cli_filesystem`` package
so that existing ``from plugins.filesystem import ...`` imports continue to
work when the package is installed (``pip install -e plugins/filesystem/``).
"""

from __future__ import annotations

from qodalis_cli_filesystem import (
    FileEntry,
    FileStat,
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
    IFileStorageProvider,
)

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
