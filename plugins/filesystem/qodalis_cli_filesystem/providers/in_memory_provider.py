"""In-memory file storage provider backed by a tree of nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

from ..errors import (
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
)
from ..i_file_storage_provider import IFileStorageProvider
from ..models import FileEntry, FileStat


@dataclass
class _FileNode:
    """Internal tree node representing a file or directory."""

    name: str
    type: str  # 'file' or 'directory'
    content: bytes = b""
    children: dict[str, _FileNode] = field(default_factory=dict)
    created_at: str = ""
    modified_at: str = ""
    size: int = 0
    permissions: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            now = datetime.now(timezone.utc).isoformat()
            self.created_at = now
            self.modified_at = now
        if self.type == "directory":
            self.permissions = "drwxr-xr-x"
        else:
            self.permissions = "-rw-r--r--"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: str) -> str:
    """Normalize *path* to a canonical form (strip leading/trailing slashes)."""
    # Treat empty, "/", "." as root
    path = path.strip("/").strip()
    if path in ("", "."):
        return ""
    # Collapse consecutive slashes and resolve "." / ".."
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts)


class InMemoryFileStorageProvider(IFileStorageProvider):
    """In-memory file storage backed by a tree of ``_FileNode`` objects."""

    def __init__(self) -> None:
        self._root = _FileNode(name="", type="directory")

    @property
    def name(self) -> str:
        return "in-memory"

    async def list(self, path: str) -> list[FileEntry]:
        node = self._resolve(_normalize_path(path))
        if node is None:
            raise FileStorageNotFoundError(path)
        if node.type != "directory":
            raise FileStorageNotADirectoryError(path)

        entries: list[FileEntry] = []
        for child in sorted(node.children.values(), key=lambda n: n.name):
            entries.append(
                FileEntry(
                    name=child.name,
                    type=child.type,
                    size=child.size,
                    modified=child.modified_at,
                    permissions=child.permissions,
                )
            )
        return entries

    async def read_file(self, path: str) -> str:
        node = self._resolve(_normalize_path(path))
        if node is None:
            raise FileStorageNotFoundError(path)
        if node.type == "directory":
            raise FileStorageIsADirectoryError(path)
        return node.content.decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str | bytes) -> None:
        norm = _normalize_path(path)
        if not norm:
            raise FileStorageIsADirectoryError(path)

        parts = norm.split("/")
        parent = self._ensure_parents(parts[:-1], path)
        file_name = parts[-1]

        raw = content.encode("utf-8") if isinstance(content, str) else content
        now = _now_iso()

        existing = parent.children.get(file_name)
        if existing is not None and existing.type == "directory":
            raise FileStorageIsADirectoryError(path)

        if existing is not None:
            existing.content = raw
            existing.size = len(raw)
            existing.modified_at = now
        else:
            parent.children[file_name] = _FileNode(
                name=file_name,
                type="file",
                content=raw,
                created_at=now,
                modified_at=now,
                size=len(raw),
            )

    async def stat(self, path: str) -> FileStat:
        node = self._resolve(_normalize_path(path))
        if node is None:
            raise FileStorageNotFoundError(path)
        return FileStat(
            name=node.name,
            type=node.type,
            size=node.size,
            created=node.created_at,
            modified=node.modified_at,
            permissions=node.permissions,
        )

    async def mkdir(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if not norm:
            return  # root always exists

        parts = norm.split("/")
        if recursive:
            self._ensure_parents(parts, path)
        else:
            parent = self._ensure_parents(parts[:-1], path, create=False)
            dir_name = parts[-1]
            existing = parent.children.get(dir_name)
            if existing is not None:
                if existing.type != "directory":
                    raise FileStorageExistsError(path)
                return  # already exists
            parent.children[dir_name] = _FileNode(name=dir_name, type="directory")

    async def remove(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if not norm:
            raise FileStoragePermissionError(path)

        parts = norm.split("/")
        parent = self._resolve("/".join(parts[:-1]))
        if parent is None or parts[-1] not in parent.children:
            raise FileStorageNotFoundError(path)

        node = parent.children[parts[-1]]
        if node.type == "directory" and node.children and not recursive:
            raise FileStorageNotADirectoryError(path)

        del parent.children[parts[-1]]

    async def copy(self, src: str, dest: str) -> None:
        src_node = self._resolve(_normalize_path(src))
        if src_node is None:
            raise FileStorageNotFoundError(src)

        dest_norm = _normalize_path(dest)
        if not dest_norm:
            raise FileStorageIsADirectoryError(dest)

        parts = dest_norm.split("/")
        parent = self._ensure_parents(parts[:-1], dest)
        parent.children[parts[-1]] = self._deep_copy(src_node, parts[-1])

    async def move(self, src: str, dest: str) -> None:
        src_norm = _normalize_path(src)
        if not src_norm:
            raise FileStoragePermissionError(src)

        src_node = self._resolve(src_norm)
        if src_node is None:
            raise FileStorageNotFoundError(src)

        dest_norm = _normalize_path(dest)
        if not dest_norm:
            raise FileStorageIsADirectoryError(dest)

        dest_parts = dest_norm.split("/")
        dest_parent = self._ensure_parents(dest_parts[:-1], dest)

        # Copy node to new location
        dest_parent.children[dest_parts[-1]] = self._deep_copy(
            src_node, dest_parts[-1]
        )

        # Remove from old location
        src_parts = src_norm.split("/")
        src_parent = self._resolve("/".join(src_parts[:-1]))
        if src_parent is not None:
            del src_parent.children[src_parts[-1]]

    async def exists(self, path: str) -> bool:
        return self._resolve(_normalize_path(path)) is not None

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        node = self._resolve(_normalize_path(path))
        if node is None:
            raise FileStorageNotFoundError(path)
        if node.type == "directory":
            raise FileStorageIsADirectoryError(path)

        async def _stream() -> AsyncIterator[bytes]:
            yield node.content

        return _stream()

    async def upload_file(self, path: str, content: bytes) -> None:
        await self.write_file(path, content)

    def _resolve(self, norm_path: str) -> _FileNode | None:
        """Walk the tree and return the node at *norm_path*, or ``None``."""
        if not norm_path:
            return self._root
        node = self._root
        for part in norm_path.split("/"):
            if node.type != "directory" or part not in node.children:
                return None
            node = node.children[part]
        return node

    def _ensure_parents(
        self, parts: list[str], original_path: str, *, create: bool = True
    ) -> _FileNode:
        """Ensure all parent directories exist for *parts*.

        Returns the final parent node.  If *create* is ``False``, raises
        ``FileStorageNotFoundError`` when a segment is missing.
        """
        node = self._root
        for part in parts:
            if part not in node.children:
                if not create:
                    raise FileStorageNotFoundError(original_path)
                node.children[part] = _FileNode(name=part, type="directory")
            child = node.children[part]
            if child.type != "directory":
                raise FileStorageNotADirectoryError(original_path)
            node = child
        return node

    def _deep_copy(self, node: _FileNode, new_name: str) -> _FileNode:
        """Recursively copy a node tree, assigning *new_name* to the root copy."""
        now = _now_iso()
        copy = _FileNode(
            name=new_name,
            type=node.type,
            content=bytes(node.content),
            created_at=now,
            modified_at=now,
            size=node.size,
            permissions=node.permissions,
        )
        for child_name, child_node in node.children.items():
            copy.children[child_name] = self._deep_copy(child_node, child_name)
        return copy
