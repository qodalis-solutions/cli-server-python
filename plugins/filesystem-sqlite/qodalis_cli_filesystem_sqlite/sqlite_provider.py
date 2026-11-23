"""SQLite-backed file storage provider."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

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
from qodalis_cli_migrations import (
    CliMigration,
    CliMigrationDatabaseOptions,
    CliMigrationRunner,
    CliMigrationRunOptions,
    CliSqlConnection,
    CliSqlSession,
    CodeSource,
    adopt_legacy_table,
    code_source,
    sqlite_connection,
    table_exists,
)
from qodalis_cli_server_abstractions import CliTableNameOptions, qcli_table_name

#: The columns of a previous release (``files``), renamed by the first migration.
LEGACY_COLUMNS: dict[str, str] = {
    "id": "Id",
    "path": "Path",
    "name": "Name",
    "type": "Type",
    "content": "Content",
    "size": "Size",
    "permissions": "Permissions",
    "created_at": "CreatedAt",
    "modified_at": "ModifiedAt",
    "parent_path": "ParentPath",
}


def filesystem_sqlite_migrations(table: str, legacy: bool = False) -> CodeSource:
    """The schema of the files table as migrations (source ``qcli-filesystem``); ``table`` is unquoted."""

    async def files_table(db: CliSqlSession) -> None:
        if not legacy:
            await adopt_legacy_table(db, "files", table, LEGACY_COLUMNS)
        if await table_exists(db, table):
            return
        col = (lambda n: n.lower()) if legacy else (lambda n: n)
        created, modified, parent = (
            ("created_at", "modified_at", "parent_path") if legacy else ("CreatedAt", "ModifiedAt", "ParentPath")
        )
        await db.exec(f"""CREATE TABLE "{table}" (
            "{col('Id')}" INTEGER PRIMARY KEY AUTOINCREMENT,
            "{col('Path')}" TEXT NOT NULL UNIQUE,
            "{col('Name')}" TEXT NOT NULL,
            "{col('Type')}" TEXT NOT NULL CHECK("{col('Type')}" IN ('file', 'directory')),
            "{col('Content')}" TEXT,
            "{col('Size')}" INTEGER NOT NULL DEFAULT 0,
            "{col('Permissions')}" TEXT DEFAULT '644',
            "{created}" TEXT NOT NULL,
            "{modified}" TEXT NOT NULL,
            "{parent}" TEXT
        )""")

    async def parent_index(db: CliSqlSession) -> None:
        if legacy:
            await db.exec(f'CREATE INDEX IF NOT EXISTS "idx_files_parent" ON "{table}" ("parent_path")')
            return
        await db.exec('DROP INDEX IF EXISTS "idx_files_parent"')
        await db.exec(f'CREATE INDEX IF NOT EXISTS "IX_{table}_ParentPath" ON "{table}" ("ParentPath")')

    return code_source("qcli-filesystem", [
        CliMigration("1", "files table", files_table),
        CliMigration("2", "index on the parent path", parent_index),
    ])


@dataclass
class SqliteProviderOptions:
    """Configuration for the SQLite file storage provider."""

    db_path: str = "./data/files.db"
    #: Prefix of the table (default ``Qcli``: ``QcliFiles``); ``""`` gives ``Files``.
    table_prefix: str | None = None
    #: The table's name (``{"Files": "MyFiles"}``), winning over the prefix.
    tables: dict[str, str] = field(default_factory=dict)
    #: Keep the previous release's ``files`` table and columns instead of renaming them.
    legacy_table_names: bool = False


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

    The table is ``QcliFiles`` (PascalCase columns), versioned by the migrations
    plugin (source ``qcli-filesystem``): the first operation after an upgrade
    renames the previous release's ``files`` table and its columns, keeping the
    data.  ``connection`` and ``migration_source`` show it in the migrations
    plugin.
    """

    def __init__(self, options: SqliteProviderOptions | None = None) -> None:
        if options is None:
            options = SqliteProviderOptions()
        self._db_path = options.db_path

        if self._db_path != ":memory:":
            parent_dir = os.path.dirname(self._db_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

        legacy = options.legacy_table_names
        name = qcli_table_name(
            "Files",
            CliTableNameOptions(
                table_prefix=options.table_prefix, tables=dict(options.tables or {}), legacy_table_names=legacy,
            ),
            "files",
        )
        #: The files table, quoted, and its columns with an underscore in the previous release.
        self._t = f'"{name}"'
        created, modified, parent = (
            ("created_at", "modified_at", "parent_path") if legacy else ("CreatedAt", "ModifiedAt", "ParentPath")
        )
        self._created, self._modified, self._parent = f'"{created}"', f'"{modified}"', f'"{parent}"'
        #: Every column, aliased to the names the provider reads.
        self._row = (
            f"id AS id, path AS path, name AS name, type AS type, content AS content, size AS size, "
            f"permissions AS permissions, {self._created} AS created_at, {self._modified} AS modified_at, "
            f"{self._parent} AS parent_path"
        )
        self._insert = (
            f"INSERT INTO {self._t} (path, name, type, content, size, permissions, {self._created}, "
            f"{self._modified}, {self._parent}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        #: The database, for the migrations plugin (``add_database("files", ...)``).
        self.connection: CliSqlConnection = sqlite_connection(self._conn)
        #: The files table's migrations (``qcli-filesystem``), to show them in the migrations plugin.
        self.migration_source: CodeSource = filesystem_sqlite_migrations(name, legacy)
        self._runner = CliMigrationRunner(
            "files", CliMigrationDatabaseOptions(self.connection, table_prefix=options.table_prefix),
        ).add_source(self.migration_source)
        self._ready = False
        self._ready_lock: asyncio.Lock | None = None

    async def _ensure_ready(self) -> None:
        """Migrates the schema and creates the root, once, before the first operation."""
        if self._ready:
            return
        if self._ready_lock is None:
            self._ready_lock = asyncio.Lock()
        async with self._ready_lock:
            if self._ready:
                return
            run = await self._runner.up(CliMigrationRunOptions(trigger="startup", wait=True))
            if getattr(run, "status", None) == "failed":
                raise RuntimeError(f"filesystem-sqlite schema: {run.error}")  # type: ignore[union-attr]
            self._ensure_root()
            self._ready = True

    def _ensure_root(self) -> None:
        """Insert the root directory row if it does not exist."""
        row = self._conn.execute(f"SELECT id FROM {self._t} WHERE path = ?", ("/",)).fetchone()
        if row is None:
            now = _now_iso()
            self._conn.execute(self._insert, ("/", "", "directory", None, 0, "drwxr-xr-x", now, now, None))
            self._conn.commit()

    def _get(self, path: str) -> sqlite3.Row | None:
        return self._conn.execute(f"SELECT {self._row} FROM {self._t} WHERE path = ?", (path,)).fetchone()

    @property
    def name(self) -> str:
        return "sqlite"

    async def list(self, path: str) -> list[FileEntry]:
        await self._ensure_ready()
        norm = _normalize_path(path)
        row = self._get(norm)
        if row is None:
            raise FileStorageNotFoundError(path)
        if row["type"] != "directory":
            raise FileStorageNotADirectoryError(path)

        rows = self._conn.execute(
            f"SELECT {self._row} FROM {self._t} WHERE {self._parent} = ? ORDER BY name",
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
        await self._ensure_ready()
        norm = _normalize_path(path)
        row = self._get(norm)
        if row is None:
            raise FileStorageNotFoundError(path)
        if row["type"] == "directory":
            raise FileStorageIsADirectoryError(path)
        return row["content"] or ""

    async def write_file(self, path: str, content: str | bytes) -> None:
        await self._ensure_ready()
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
        existing = self._get(norm)
        if existing is not None and existing["type"] == "directory":
            raise FileStorageIsADirectoryError(path)

        if existing is not None:
            self._conn.execute(
                f"UPDATE {self._t} SET content = ?, size = ?, {self._modified} = ? WHERE path = ?",
                (text, size, now, norm),
            )
        else:
            self._conn.execute(self._insert, (norm, file_name, "file", text, size, "-rw-r--r--", now, now, parent))
        self._conn.commit()

    async def stat(self, path: str) -> FileStat:
        await self._ensure_ready()
        norm = _normalize_path(path)
        row = self._get(norm)
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
        await self._ensure_ready()
        norm = _normalize_path(path)
        if norm == "/":
            return  # root always exists

        if recursive:
            await self._ensure_parents(norm)
        else:
            parent = _parent_path(norm)
            # Check parent exists
            if parent:
                p_row = self._get(parent)
                if p_row is None:
                    raise FileStorageNotFoundError(path)
                if p_row["type"] != "directory":
                    raise FileStorageNotADirectoryError(path)

            existing = self._get(norm)
            if existing is not None:
                if existing["type"] != "directory":
                    raise FileStorageExistsError(path)
                return  # already exists

            dir_name = norm.rsplit("/", 1)[-1]
            now = _now_iso()
            self._conn.execute(self._insert, (norm, dir_name, "directory", None, 0, "drwxr-xr-x", now, now, parent))
            self._conn.commit()

    async def remove(self, path: str, recursive: bool = False) -> None:
        await self._ensure_ready()
        norm = _normalize_path(path)
        if norm == "/":
            raise FileStoragePermissionError(path)

        row = self._get(norm)
        if row is None:
            raise FileStorageNotFoundError(path)

        if row["type"] == "directory" and not recursive:
            # Check if directory has children
            child = self._conn.execute(
                f"SELECT id FROM {self._t} WHERE {self._parent} = ? LIMIT 1", (norm,)
            ).fetchone()
            if child is not None:
                raise FileStorageNotADirectoryError(path)

        if recursive:
            # Delete the entry and all descendants (path starts with norm/)
            self._conn.execute(f"DELETE FROM {self._t} WHERE path = ? OR path LIKE ?", (norm, norm + "/%"))
        else:
            self._conn.execute(f"DELETE FROM {self._t} WHERE path = ?", (norm,))
        self._conn.commit()

    async def copy(self, src: str, dest: str) -> None:
        await self._ensure_ready()
        src_norm = _normalize_path(src)
        dest_norm = _normalize_path(dest)

        if dest_norm == "/":
            raise FileStorageIsADirectoryError(dest)

        src_row = self._get(src_norm)
        if src_row is None:
            raise FileStorageNotFoundError(src)

        dest_parent = _parent_path(dest_norm)
        if dest_parent and dest_parent != "/":
            await self._ensure_parents(dest_parent)

        now = _now_iso()
        dest_name = dest_norm.rsplit("/", 1)[-1]

        # Remove existing destination if any
        self._conn.execute(f"DELETE FROM {self._t} WHERE path = ?", (dest_norm,))

        # Copy the source entry
        self._conn.execute(self._insert, (
            dest_norm, dest_name, src_row["type"], src_row["content"],
            src_row["size"], src_row["permissions"], now, now, dest_parent,
        ))

        # If directory, copy all descendants
        if src_row["type"] == "directory":
            descendants = self._conn.execute(
                f"SELECT {self._row} FROM {self._t} WHERE path LIKE ?", (src_norm + "/%",)
            ).fetchall()
            for desc in descendants:
                new_path = dest_norm + desc["path"][len(src_norm):]
                new_parent = _parent_path(new_path)
                # Remove existing at new path
                self._conn.execute(f"DELETE FROM {self._t} WHERE path = ?", (new_path,))
                self._conn.execute(self._insert, (
                    new_path, desc["name"], desc["type"], desc["content"],
                    desc["size"], desc["permissions"], now, now, new_parent,
                ))

        self._conn.commit()

    async def move(self, src: str, dest: str) -> None:
        await self._ensure_ready()
        src_norm = _normalize_path(src)
        if src_norm == "/":
            raise FileStoragePermissionError(src)

        dest_norm = _normalize_path(dest)
        if dest_norm == "/":
            raise FileStorageIsADirectoryError(dest)

        if self._get(src_norm) is None:
            raise FileStorageNotFoundError(src)

        # Copy then remove source
        await self.copy(src, dest)

        # Remove source and its descendants
        self._conn.execute(f"DELETE FROM {self._t} WHERE path = ? OR path LIKE ?", (src_norm, src_norm + "/%"))
        self._conn.commit()

    async def exists(self, path: str) -> bool:
        await self._ensure_ready()
        return self._get(_normalize_path(path)) is not None

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        await self._ensure_ready()
        norm = _normalize_path(path)
        row = self._get(norm)
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
            existing = self._get(current)
            if existing is not None:
                if existing["type"] != "directory":
                    raise FileStorageNotADirectoryError(current)
                continue
            now = _now_iso()
            self._conn.execute(self._insert, (current, part, "directory", None, 0, "drwxr-xr-x", now, now, _parent_path(current)))
        self._conn.commit()
