from __future__ import annotations

import os
import shutil
import stat as stat_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

from ..errors import (
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
)
from ..i_file_storage_provider import IFileStorageProvider
from ..models import FileEntry, FileStat


@dataclass
class OsProviderOptions:
    """Configuration for :class:`OsFileStorageProvider`.

    Attributes:
        allowed_paths: Absolute directory paths that clients may access.
            Each requested path is resolved and checked against this
            whitelist before any I/O is performed.
    """

    allowed_paths: list[str] = field(default_factory=list)


class OsFileStorageProvider(IFileStorageProvider):
    """File storage provider backed by the operating system filesystem.

    Access is restricted to the directories listed in *options.allowed_paths*.
    """

    def __init__(self, options: OsProviderOptions) -> None:
        self._allowed_paths = [os.path.realpath(p) for p in options.allowed_paths]

    # -- IFileStorageProvider interface ----------------------------------------

    @property
    def name(self) -> str:
        return "os"

    async def list(self, path: str) -> list[FileEntry]:
        resolved = self._validate(path)

        if not os.path.exists(resolved):
            raise FileStorageNotFoundError(path)
        if not os.path.isdir(resolved):
            raise FileStorageNotADirectoryError(path)

        entries: list[FileEntry] = []
        for entry_name in sorted(os.listdir(resolved)):
            full = os.path.join(resolved, entry_name)
            try:
                st = os.stat(full)
                entry_type = "directory" if stat_mod.S_ISDIR(st.st_mode) else "file"
                entries.append(
                    FileEntry(
                        name=entry_name,
                        type=entry_type,
                        size=st.st_size,
                        modified=datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        permissions=stat_mod.filemode(st.st_mode),
                    )
                )
            except OSError:
                continue
        return entries

    async def read_file(self, path: str) -> str:
        resolved = self._validate(path)

        if not os.path.exists(resolved):
            raise FileStorageNotFoundError(path)
        if os.path.isdir(resolved):
            raise FileStorageIsADirectoryError(path)

        with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    async def write_file(self, path: str, content: str | bytes) -> None:
        resolved = self._validate(path)

        if os.path.isdir(resolved):
            raise FileStorageIsADirectoryError(path)

        parent = os.path.dirname(resolved)
        if not os.path.isdir(parent):
            raise FileStorageNotFoundError(os.path.dirname(path))

        if isinstance(content, str):
            with open(resolved, "w", encoding="utf-8") as fh:
                fh.write(content)
        else:
            with open(resolved, "wb") as fh:
                fh.write(content)

    async def stat(self, path: str) -> FileStat:
        resolved = self._validate(path)

        if not os.path.exists(resolved):
            raise FileStorageNotFoundError(path)

        st = os.stat(resolved)
        entry_type = "directory" if stat_mod.S_ISDIR(st.st_mode) else "file"
        return FileStat(
            name=os.path.basename(resolved),
            type=entry_type,
            size=st.st_size,
            created=datetime.fromtimestamp(
                st.st_ctime, tz=timezone.utc
            ).isoformat(),
            modified=datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc
            ).isoformat(),
            permissions=stat_mod.filemode(st.st_mode),
        )

    async def mkdir(self, path: str, recursive: bool = False) -> None:
        resolved = self._validate(path)

        if recursive:
            os.makedirs(resolved, exist_ok=True)
        else:
            parent = os.path.dirname(resolved)
            if not os.path.isdir(parent):
                raise FileStorageNotFoundError(os.path.dirname(path))
            if os.path.exists(resolved):
                return  # already exists
            os.mkdir(resolved)

    async def remove(self, path: str, recursive: bool = False) -> None:
        resolved = self._validate(path)

        if not os.path.exists(resolved):
            raise FileStorageNotFoundError(path)

        if os.path.isdir(resolved):
            if recursive:
                shutil.rmtree(resolved)
            else:
                os.rmdir(resolved)
        else:
            os.remove(resolved)

    async def copy(self, src: str, dest: str) -> None:
        resolved_src = self._validate(src)
        resolved_dest = self._validate(dest)

        if not os.path.exists(resolved_src):
            raise FileStorageNotFoundError(src)

        if os.path.isdir(resolved_src):
            shutil.copytree(resolved_src, resolved_dest)
        else:
            shutil.copy2(resolved_src, resolved_dest)

    async def move(self, src: str, dest: str) -> None:
        resolved_src = self._validate(src)
        resolved_dest = self._validate(dest)

        if not os.path.exists(resolved_src):
            raise FileStorageNotFoundError(src)

        shutil.move(resolved_src, resolved_dest)

    async def exists(self, path: str) -> bool:
        resolved = self._validate(path)
        return os.path.exists(resolved)

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        resolved = self._validate(path)

        if not os.path.exists(resolved):
            raise FileStorageNotFoundError(path)
        if os.path.isdir(resolved):
            raise FileStorageIsADirectoryError(path)

        async def _stream() -> AsyncIterator[bytes]:
            with open(resolved, "rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return _stream()

    async def upload_file(self, path: str, content: bytes) -> None:
        await self.write_file(path, content)

    # -- Private helpers -------------------------------------------------------

    def _validate(self, path: str) -> str:
        """Resolve *path* and check it against the allowed whitelist.

        Returns the resolved absolute path.

        Raises:
            FileStoragePermissionError: If the path is outside allowed dirs.
        """
        resolved = os.path.realpath(path)

        for allowed in self._allowed_paths:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved

        raise FileStoragePermissionError(path)
