from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from .models import FileEntry, FileStat


class IFileStorageProvider(ABC):
    """Abstract base class for file storage providers.

    All methods are async to support both local and remote backends.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this provider."""
        ...

    @abstractmethod
    async def list(self, path: str) -> list[FileEntry]:
        """List entries in a directory."""
        ...

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """Read a file and return its text content."""
        ...

    @abstractmethod
    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write content to a file, creating or overwriting it."""
        ...

    @abstractmethod
    async def stat(self, path: str) -> FileStat:
        """Return metadata for a file or directory."""
        ...

    @abstractmethod
    async def mkdir(self, path: str, recursive: bool = False) -> None:
        """Create a directory."""
        ...

    @abstractmethod
    async def remove(self, path: str, recursive: bool = False) -> None:
        """Remove a file or directory."""
        ...

    @abstractmethod
    async def copy(self, src: str, dest: str) -> None:
        """Copy a file or directory from *src* to *dest*."""
        ...

    @abstractmethod
    async def move(self, src: str, dest: str) -> None:
        """Move (rename) a file or directory from *src* to *dest*."""
        ...

    @abstractmethod
    async def exists(self, path: str) -> bool:
        """Return True if the path exists."""
        ...

    @abstractmethod
    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        """Yield the file content as an async byte stream."""
        ...

    @abstractmethod
    async def upload_file(self, path: str, content: bytes) -> None:
        """Upload raw bytes to the given path."""
        ...
