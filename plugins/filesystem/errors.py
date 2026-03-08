"""Backward-compatible re-export from ``qodalis_cli_filesystem``."""

from __future__ import annotations

from qodalis_cli_filesystem.errors import (  # noqa: F401
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
)
