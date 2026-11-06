"""A file storage provider that publishes ``fs:changed`` after every change."""

from __future__ import annotations

from typing import Any, AsyncIterator

from qodalis_cli_filesystem import FileEntry, FileStat, IFileStorageProvider
from qodalis_cli_server_abstractions.events import CliEventAudience, ICliEventBus

#: Permission of the ``fs:changed`` event's audience.
FILES_READ = "files:read"


class EventPublishingFileStorageProvider(IFileStorageProvider):
    """Wraps the server's file storage provider: the filesystem API
    (``/api/qcli/fs``) and the MCP ``fs_*`` tools both go through it, so each
    change publishes one ``fs:changed`` (``action``: ``upload``, ``mkdir``,
    ``delete``, ``write``, ``move`` or ``copy``; ``path``; ``newPath`` for a
    move or copy) to the sockets with ``files:read``.  Everything else is the
    wrapped provider's."""

    def __init__(self, inner: IFileStorageProvider, bus: ICliEventBus) -> None:
        self.inner = inner
        self._bus = bus

    def __getattr__(self, name: str) -> Any:
        # Provider-specific members (e.g. an OS provider's roots).
        return getattr(self.inner, name)

    async def _changed(self, action: str, path: str, new_path: str | None = None) -> None:
        data: dict[str, Any] = {"action": action, "path": path}
        if new_path is not None:
            data["newPath"] = new_path
        await self._bus.publish("fs:changed", data, CliEventAudience(permission=FILES_READ))

    @property
    def name(self) -> str:
        return self.inner.name

    async def list(self, path: str) -> list[FileEntry]:
        return await self.inner.list(path)

    async def read_file(self, path: str) -> str:
        return await self.inner.read_file(path)

    async def write_file(self, path: str, content: str | bytes) -> None:
        await self.inner.write_file(path, content)
        await self._changed("write", path)

    async def stat(self, path: str) -> FileStat:
        return await self.inner.stat(path)

    async def mkdir(self, path: str, recursive: bool = False) -> None:
        await self.inner.mkdir(path, recursive=recursive)
        await self._changed("mkdir", path)

    async def remove(self, path: str, recursive: bool = False) -> None:
        await self.inner.remove(path, recursive=recursive)
        await self._changed("delete", path)

    async def copy(self, src: str, dest: str) -> None:
        await self.inner.copy(src, dest)
        await self._changed("copy", src, dest)

    async def move(self, src: str, dest: str) -> None:
        await self.inner.move(src, dest)
        await self._changed("move", src, dest)

    async def exists(self, path: str) -> bool:
        return await self.inner.exists(path)

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        return await self.inner.get_download_stream(path)

    async def upload_file(self, path: str, content: bytes) -> None:
        await self.inner.upload_file(path, content)
        await self._changed("upload", path)
