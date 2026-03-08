from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileEntry:
    """Represents a single entry in a directory listing."""

    name: str
    type: str  # 'file' or 'directory'
    size: int
    modified: str  # ISO 8601
    permissions: str | None = None


@dataclass
class FileStat:
    """Detailed metadata for a file or directory."""

    name: str
    type: str  # 'file' or 'directory'
    size: int
    created: str  # ISO 8601
    modified: str  # ISO 8601
    permissions: str | None = None
