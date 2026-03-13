from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

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


@dataclass
class JsonFileProviderOptions:
    """Configuration for the JSON file storage provider."""

    file_path: str


@dataclass
class _FileNode:
    """Internal tree node representing a file or directory."""

    name: str
    type: str  # 'file' or 'directory'
    content: str = ""  # base64 or plain text stored as string
    children: list[_FileNode] = field(default_factory=list)
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize node to a JSON-compatible dict."""
        return {
            "name": self.name,
            "type": self.type,
            "content": self.content,
            "children": [child.to_dict() for child in self.children],
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "size": self.size,
            "permissions": self.permissions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _FileNode:
        """Deserialize a node from a dict."""
        node = cls(
            name=data["name"],
            type=data["type"],
            content=data.get("content", ""),
            created_at=data.get("created_at", ""),
            modified_at=data.get("modified_at", ""),
            size=data.get("size", 0),
            permissions=data.get("permissions"),
        )
        node.children = [
            cls.from_dict(child) for child in data.get("children", [])
        ]
        return node

    def get_child(self, name: str) -> _FileNode | None:
        """Find a child by name."""
        for child in self.children:
            if child.name == name:
                return child
        return None

    def add_child(self, child: _FileNode) -> None:
        """Add a child node."""
        self.children.append(child)

    def remove_child(self, name: str) -> None:
        """Remove a child by name."""
        self.children = [c for c in self.children if c.name != name]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: str) -> str:
    """Normalize *path* to a canonical form (strip leading/trailing slashes)."""
    path = path.strip("/").strip()
    if path in ("", "."):
        return ""
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


class JsonFileStorageProvider(IFileStorageProvider):
    """File storage provider backed by a JSON file on disk.

    Uses the same tree structure as ``InMemoryFileStorageProvider`` but
    persists all data to a JSON file after every write mutation.
    """

    def __init__(self, options: JsonFileProviderOptions) -> None:
        self._file_path = options.file_path
        self._root = self._load()

    # -- Persistence -----------------------------------------------------------

    def _load(self) -> _FileNode:
        """Read the JSON file and deserialize the tree.

        If the file does not exist, return an empty root node.
        """
        if not os.path.exists(self._file_path):
            return _FileNode(name="", type="directory")
        with open(self._file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _FileNode.from_dict(data)

    def _save(self) -> None:
        """Serialize the tree to JSON and write it to disk.

        Creates parent directories if they do not exist.
        """
        parent_dir = os.path.dirname(self._file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(self._root.to_dict(), f, indent=2)

    # -- IFileStorageProvider interface ----------------------------------------

    @property
    def name(self) -> str:
        return "json-file"

    async def list(self, path: str) -> list[FileEntry]:
        node = self._resolve(_normalize_path(path))
        if node is None:
            raise FileStorageNotFoundError(path)
        if node.type != "directory":
            raise FileStorageNotADirectoryError(path)

        entries: list[FileEntry] = []
        for child in sorted(node.children, key=lambda n: n.name):
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
        return node.content

    async def write_file(self, path: str, content: str | bytes) -> None:
        norm = _normalize_path(path)
        if not norm:
            raise FileStorageIsADirectoryError(path)

        parts = norm.split("/")
        parent = self._ensure_parents(parts[:-1], path)
        file_name = parts[-1]

        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        now = _now_iso()

        existing = parent.get_child(file_name)
        if existing is not None and existing.type == "directory":
            raise FileStorageIsADirectoryError(path)

        if existing is not None:
            existing.content = text
            existing.size = len(text.encode("utf-8"))
            existing.modified_at = now
        else:
            parent.add_child(
                _FileNode(
                    name=file_name,
                    type="file",
                    content=text,
                    created_at=now,
                    modified_at=now,
                    size=len(text.encode("utf-8")),
                )
            )
        self._save()

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
            existing = parent.get_child(dir_name)
            if existing is not None:
                if existing.type != "directory":
                    raise FileStorageExistsError(path)
                return  # already exists
            parent.add_child(_FileNode(name=dir_name, type="directory"))
        self._save()

    async def remove(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if not norm:
            raise FileStoragePermissionError(path)

        parts = norm.split("/")
        parent = self._resolve("/".join(parts[:-1]))
        if parent is None or parent.get_child(parts[-1]) is None:
            raise FileStorageNotFoundError(path)

        node = parent.get_child(parts[-1])
        if node is not None and node.type == "directory" and node.children and not recursive:
            raise FileStorageNotADirectoryError(path)

        parent.remove_child(parts[-1])
        self._save()

    async def copy(self, src: str, dest: str) -> None:
        src_node = self._resolve(_normalize_path(src))
        if src_node is None:
            raise FileStorageNotFoundError(src)

        dest_norm = _normalize_path(dest)
        if not dest_norm:
            raise FileStorageIsADirectoryError(dest)

        parts = dest_norm.split("/")
        parent = self._ensure_parents(parts[:-1], dest)
        # Remove existing child with same name if any
        parent.remove_child(parts[-1])
        parent.add_child(self._deep_copy(src_node, parts[-1]))
        self._save()

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
        dest_parent.remove_child(dest_parts[-1])
        dest_parent.add_child(self._deep_copy(src_node, dest_parts[-1]))

        # Remove from old location
        src_parts = src_norm.split("/")
        src_parent = self._resolve("/".join(src_parts[:-1]))
        if src_parent is not None:
            src_parent.remove_child(src_parts[-1])
        self._save()

    async def exists(self, path: str) -> bool:
        return self._resolve(_normalize_path(path)) is not None

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        node = self._resolve(_normalize_path(path))
        if node is None:
            raise FileStorageNotFoundError(path)
        if node.type == "directory":
            raise FileStorageIsADirectoryError(path)

        async def _stream() -> AsyncIterator[bytes]:
            yield node.content.encode("utf-8")

        return _stream()

    async def upload_file(self, path: str, content: bytes) -> None:
        await self.write_file(path, content)

    # -- Private helpers -------------------------------------------------------

    def _resolve(self, norm_path: str) -> _FileNode | None:
        """Walk the tree and return the node at *norm_path*, or ``None``."""
        if not norm_path:
            return self._root
        node = self._root
        for part in norm_path.split("/"):
            child = node.get_child(part)
            if child is None or node.type != "directory":
                return None
            node = child
        return node

    def _ensure_parents(
        self, parts: list[str], original_path: str, *, create: bool = True
    ) -> _FileNode:
        """Ensure all parent directories exist for *parts*.

        Returns the final parent node. If *create* is ``False``, raises
        ``FileStorageNotFoundError`` when a segment is missing.
        """
        node = self._root
        for part in parts:
            child = node.get_child(part)
            if child is None:
                if not create:
                    raise FileStorageNotFoundError(original_path)
                new_dir = _FileNode(name=part, type="directory")
                node.add_child(new_dir)
                child = new_dir
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
            content=node.content,
            created_at=now,
            modified_at=now,
            size=node.size,
            permissions=node.permissions,
        )
        for child in node.children:
            copy.add_child(self._deep_copy(child, child.name))
        return copy
