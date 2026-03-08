from __future__ import annotations

import importlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

_fs_errors = importlib.import_module("plugins.filesystem.errors")
_fs_iface = importlib.import_module("plugins.filesystem.i_file_storage_provider")
_fs_models = importlib.import_module("plugins.filesystem.models")

FileStorageExistsError = _fs_errors.FileStorageExistsError
FileStorageIsADirectoryError = _fs_errors.FileStorageIsADirectoryError
FileStorageNotADirectoryError = _fs_errors.FileStorageNotADirectoryError
FileStorageNotFoundError = _fs_errors.FileStorageNotFoundError
FileStoragePermissionError = _fs_errors.FileStoragePermissionError
IFileStorageProvider = _fs_iface.IFileStorageProvider
FileEntry = _fs_models.FileEntry
FileStat = _fs_models.FileStat

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('file', 'directory')),
    content TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    permissions TEXT DEFAULT '644',
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    parent_path TEXT
);
"""

_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_path);
"""


@dataclass
class SqliteProviderOptions:
    """Configuration for the SQLite file storage provider."""

    db_path: str = "./data/files.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: str) -> str:
    """Normalize *path* to a canonical form with leading slash."""
    path = path.strip()
    if path in ("", ".", "/"):
        return "/"
    # Remove trailing slash, ensure leading slash
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
        else:
            parts.append(segment)
    return "/" + "/".join(parts) if parts else "/"


def _parent_path(path: str) -> str | None:
    """Return the parent path, or None for root."""
    if path == "/":
        return None
    idx = path.rfind("/")
    if idx <= 0:
        return "/"
    return path[:idx]


class SqliteFileStorageProvider(IFileStorageProvider):
    """File storage provider backed by a SQLite database.

    Uses Python's built-in ``sqlite3`` module with no external dependencies.
    All interface methods are async but use synchronous sqlite3 calls internally.
    """

    def __init__(self, options: SqliteProviderOptions | None = None) -> None:
        if options is None:
            options = SqliteProviderOptions()
        self._db_path = options.db_path

        # Create parent directories for on-disk databases
        if self._db_path != ":memory:":
            parent_dir = os.path.dirname(self._db_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX)
        self._conn.commit()

        # Ensure root directory exists
        self._ensure_root()

    def _ensure_root(self) -> None:
        row = self._conn.execute(
            "SELECT id FROM files WHERE path = ?", ("/",)
        ).fetchone()
        if row is None:
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO files (path, name, type, size, permissions, created_at, modified_at, parent_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("/", "", "directory", 0, "drwxr-xr-x", now, now, None),
            )
            self._conn.commit()

    # -- IFileStorageProvider interface ----------------------------------------

    @property
    def name(self) -> str:
        return "sqlite"

    async def list(self, path: str) -> list[FileEntry]:
        norm = _normalize_path(path)
        row = self._conn.execute(
            "SELECT type FROM files WHERE path = ?", (norm,)
        ).fetchone()
        if row is None:
            raise FileStorageNotFoundError(path)
        if row["type"] != "directory":
            raise FileStorageNotADirectoryError(path)

        rows = self._conn.execute(
            "SELECT name, type, size, modified_at, permissions FROM files "
            "WHERE parent_path = ? ORDER BY name",
            (norm,),
        ).fetchall()

        return [
            FileEntry(
                name=r["name"],
                type=r["type"],
                size=r["size"],
                modified=r["modified_at"],
                permissions=r["permissions"],
            )
            for r in rows
        ]

    async def read_file(self, path: str) -> str:
        norm = _normalize_path(path)
        row = self._conn.execute(
            "SELECT type, content FROM files WHERE path = ?", (norm,)
        ).fetchone()
        if row is None:
            raise FileStorageNotFoundError(path)
        if row["type"] == "directory":
            raise FileStorageIsADirectoryError(path)
        return row["content"] or ""

    async def write_file(self, path: str, content: str | bytes) -> None:
        norm = _normalize_path(path)
        if norm == "/":
            raise FileStorageIsADirectoryError(path)

        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        size = len(text.encode("utf-8"))
        now = _now_iso()
        file_name = norm.rsplit("/", 1)[-1]
        parent = _parent_path(norm)

        # Ensure parent directories exist
        if parent and parent != "/":
            await self._ensure_parents(parent)

        # Check if it already exists
        existing = self._conn.execute(
            "SELECT type FROM files WHERE path = ?", (norm,)
        ).fetchone()
        if existing is not None and existing["type"] == "directory":
            raise FileStorageIsADirectoryError(path)

        if existing is not None:
            self._conn.execute(
                "UPDATE files SET content = ?, size = ?, modified_at = ? WHERE path = ?",
                (text, size, now, norm),
            )
        else:
            self._conn.execute(
                "INSERT INTO files (path, name, type, content, size, permissions, created_at, modified_at, parent_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (norm, file_name, "file", text, size, "-rw-r--r--", now, now, parent),
            )
        self._conn.commit()

    async def stat(self, path: str) -> FileStat:
        norm = _normalize_path(path)
        row = self._conn.execute(
            "SELECT name, type, size, created_at, modified_at, permissions FROM files WHERE path = ?",
            (norm,),
        ).fetchone()
        if row is None:
            raise FileStorageNotFoundError(path)
        return FileStat(
            name=row["name"],
            type=row["type"],
            size=row["size"],
            created=row["created_at"],
            modified=row["modified_at"],
            permissions=row["permissions"],
        )

    async def mkdir(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if norm == "/":
            return  # root always exists

        if recursive:
            await self._ensure_parents(norm)
        else:
            parent = _parent_path(norm)
            # Check parent exists
            if parent:
                p_row = self._conn.execute(
                    "SELECT type FROM files WHERE path = ?", (parent,)
                ).fetchone()
                if p_row is None:
                    raise FileStorageNotFoundError(path)
                if p_row["type"] != "directory":
                    raise FileStorageNotADirectoryError(path)

            existing = self._conn.execute(
                "SELECT type FROM files WHERE path = ?", (norm,)
            ).fetchone()
            if existing is not None:
                if existing["type"] != "directory":
                    raise FileStorageExistsError(path)
                return  # already exists

            dir_name = norm.rsplit("/", 1)[-1]
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO files (path, name, type, size, permissions, created_at, modified_at, parent_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (norm, dir_name, "directory", 0, "drwxr-xr-x", now, now, parent),
            )
            self._conn.commit()

    async def remove(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if norm == "/":
            raise FileStoragePermissionError(path)

        row = self._conn.execute(
            "SELECT type FROM files WHERE path = ?", (norm,)
        ).fetchone()
        if row is None:
            raise FileStorageNotFoundError(path)

        if row["type"] == "directory" and not recursive:
            # Check if directory has children
            child = self._conn.execute(
                "SELECT id FROM files WHERE parent_path = ? LIMIT 1", (norm,)
            ).fetchone()
            if child is not None:
                raise FileStorageNotADirectoryError(path)

        if recursive:
            # Delete the entry and all descendants (path starts with norm/)
            self._conn.execute(
                "DELETE FROM files WHERE path = ? OR path LIKE ?",
                (norm, norm + "/%"),
            )
        else:
            self._conn.execute("DELETE FROM files WHERE path = ?", (norm,))
        self._conn.commit()

    async def copy(self, src: str, dest: str) -> None:
        src_norm = _normalize_path(src)
        dest_norm = _normalize_path(dest)

        if dest_norm == "/":
            raise FileStorageIsADirectoryError(dest)

        src_row = self._conn.execute(
            "SELECT * FROM files WHERE path = ?", (src_norm,)
        ).fetchone()
        if src_row is None:
            raise FileStorageNotFoundError(src)

        dest_parent = _parent_path(dest_norm)
        if dest_parent and dest_parent != "/":
            await self._ensure_parents(dest_parent)

        now = _now_iso()
        dest_name = dest_norm.rsplit("/", 1)[-1]

        # Remove existing destination if any
        self._conn.execute("DELETE FROM files WHERE path = ?", (dest_norm,))

        # Copy the source entry
        self._conn.execute(
            "INSERT INTO files (path, name, type, content, size, permissions, created_at, modified_at, parent_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dest_norm, dest_name, src_row["type"], src_row["content"],
             src_row["size"], src_row["permissions"], now, now, dest_parent),
        )

        # If directory, copy all descendants
        if src_row["type"] == "directory":
            descendants = self._conn.execute(
                "SELECT * FROM files WHERE path LIKE ?", (src_norm + "/%",)
            ).fetchall()
            for desc in descendants:
                new_path = dest_norm + desc["path"][len(src_norm):]
                new_parent = _parent_path(new_path)
                # Remove existing at new path
                self._conn.execute("DELETE FROM files WHERE path = ?", (new_path,))
                self._conn.execute(
                    "INSERT INTO files (path, name, type, content, size, permissions, created_at, modified_at, parent_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_path, desc["name"], desc["type"], desc["content"],
                     desc["size"], desc["permissions"], now, now, new_parent),
                )

        self._conn.commit()

    async def move(self, src: str, dest: str) -> None:
        src_norm = _normalize_path(src)
        if src_norm == "/":
            raise FileStoragePermissionError(src)

        dest_norm = _normalize_path(dest)
        if dest_norm == "/":
            raise FileStorageIsADirectoryError(dest)

        src_row = self._conn.execute(
            "SELECT * FROM files WHERE path = ?", (src_norm,)
        ).fetchone()
        if src_row is None:
            raise FileStorageNotFoundError(src)

        # Copy then remove source
        await self.copy(src, dest)

        # Remove source and its descendants
        self._conn.execute(
            "DELETE FROM files WHERE path = ? OR path LIKE ?",
            (src_norm, src_norm + "/%"),
        )
        self._conn.commit()

    async def exists(self, path: str) -> bool:
        norm = _normalize_path(path)
        row = self._conn.execute(
            "SELECT id FROM files WHERE path = ?", (norm,)
        ).fetchone()
        return row is not None

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        norm = _normalize_path(path)
        row = self._conn.execute(
            "SELECT type, content FROM files WHERE path = ?", (norm,)
        ).fetchone()
        if row is None:
            raise FileStorageNotFoundError(path)
        if row["type"] == "directory":
            raise FileStorageIsADirectoryError(path)

        content = (row["content"] or "").encode("utf-8")

        async def _stream() -> AsyncIterator[bytes]:
            yield content

        return _stream()

    async def upload_file(self, path: str, content: bytes) -> None:
        await self.write_file(path, content)

    # -- Private helpers -------------------------------------------------------

    async def _ensure_parents(self, path: str) -> None:
        """Ensure all ancestor directories exist up to and including *path*."""
        norm = _normalize_path(path)
        if norm == "/":
            return

        # Build list of ancestors that need to exist
        parts = norm.strip("/").split("/")
        current = ""
        for part in parts:
            current = current + "/" + part
            existing = self._conn.execute(
                "SELECT type FROM files WHERE path = ?", (current,)
            ).fetchone()
            if existing is not None:
                if existing["type"] != "directory":
                    raise FileStorageNotADirectoryError(current)
                continue
            now = _now_iso()
            parent = _parent_path(current)
            self._conn.execute(
                "INSERT INTO files (path, name, type, size, permissions, created_at, modified_at, parent_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (current, part, "directory", 0, "drwxr-xr-x", now, now, parent),
            )
        self._conn.commit()
