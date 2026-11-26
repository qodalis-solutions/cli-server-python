"""A read-only view of a file storage: lists, reads and downloads; every write raises
:class:`FileStoragePermissionError` (the filesystem API answers 403)."""

from __future__ import annotations

from typing import AsyncIterator

from .errors import FileStoragePermissionError
from .i_file_storage_provider import IFileStorageProvider
from .models import FileEntry, FileStat


class ReadOnlyFileStorage(IFileStorageProvider):
    """``ReadOnlyFileStorage(storage)``: the storage's reads, its writes refused."""

    def __init__(self, inner: IFileStorageProvider) -> None:
        self.inner = inner

    @property
    def name(self) -> str:
        return self.inner.name

    async def list(self, path: str) -> list[FileEntry]:
        return await self.inner.list(path)

    async def read_file(self, path: str) -> str:
        return await self.inner.read_file(path)

    async def stat(self, path: str) -> FileStat:
        return await self.inner.stat(path)

    async def exists(self, path: str) -> bool:
        return await self.inner.exists(path)

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        async for chunk in self.inner.get_download_stream(path):
            yield chunk

    async def write_file(self, path: str, content: str | bytes) -> None:
        raise FileStoragePermissionError(f"{path} (read-only storage)")

    async def mkdir(self, path: str, recursive: bool = False) -> None:
        raise FileStoragePermissionError(f"{path} (read-only storage)")

    async def remove(self, path: str, recursive: bool = False) -> None:
        raise FileStoragePermissionError(f"{path} (read-only storage)")

    async def copy(self, src: str, dest: str) -> None:
        raise FileStoragePermissionError(f"{dest} (read-only storage)")

    async def move(self, src: str, dest: str) -> None:
        raise FileStoragePermissionError(f"{dest} (read-only storage)")

    async def upload_file(self, path: str, content: bytes) -> None:
        raise FileStoragePermissionError(f"{path} (read-only storage)")


def read_only_file_storage(storage: IFileStorageProvider) -> IFileStorageProvider:
    """The storage, read-only (an already read-only one as it is)."""
    return storage if isinstance(storage, ReadOnlyFileStorage) else ReadOnlyFileStorage(storage)
