from __future__ import annotations

import importlib
import os
import tempfile

import pytest
import pytest_asyncio

from plugins.filesystem.errors import (
    FileStorageExistsError,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    FileStoragePermissionError,
)

# The plugin directory contains a hyphen, so we use importlib.
_mod = importlib.import_module("plugins.filesystem-sqlite")
SqliteProviderOptions = _mod.SqliteProviderOptions
SqliteFileStorageProvider = _mod.SqliteFileStorageProvider


@pytest_asyncio.fixture
async def provider():
    """Create a fresh in-memory SqliteFileStorageProvider."""
    return SqliteFileStorageProvider(SqliteProviderOptions(db_path=":memory:"))


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_and_read_file(provider: SqliteFileStorageProvider):
    await provider.write_file("/hello.txt", "Hello, world!")
    content = await provider.read_file("/hello.txt")
    assert content == "Hello, world!"


@pytest.mark.asyncio
async def test_exists(provider: SqliteFileStorageProvider):
    assert not await provider.exists("/foo.txt")
    await provider.write_file("/foo.txt", "data")
    assert await provider.exists("/foo.txt")


@pytest.mark.asyncio
async def test_root_exists(provider: SqliteFileStorageProvider):
    assert await provider.exists("/")


@pytest.mark.asyncio
async def test_mkdir_and_list(provider: SqliteFileStorageProvider):
    await provider.mkdir("/docs", recursive=True)
    await provider.write_file("/docs/readme.txt", "Read me")
    entries = await provider.list("/docs")
    assert len(entries) == 1
    assert entries[0].name == "readme.txt"
    assert entries[0].type == "file"


@pytest.mark.asyncio
async def test_list_root(provider: SqliteFileStorageProvider):
    await provider.write_file("/a.txt", "a")
    await provider.write_file("/b.txt", "b")
    entries = await provider.list("/")
    names = [e.name for e in entries]
    assert "a.txt" in names
    assert "b.txt" in names


@pytest.mark.asyncio
async def test_stat(provider: SqliteFileStorageProvider):
    await provider.write_file("/file.txt", "content")
    st = await provider.stat("/file.txt")
    assert st.name == "file.txt"
    assert st.type == "file"
    assert st.size == len("content".encode("utf-8"))
    assert st.permissions == "-rw-r--r--"


@pytest.mark.asyncio
async def test_stat_directory(provider: SqliteFileStorageProvider):
    await provider.mkdir("/mydir")
    st = await provider.stat("/mydir")
    assert st.name == "mydir"
    assert st.type == "directory"
    assert st.permissions == "drwxr-xr-x"


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_file(provider: SqliteFileStorageProvider):
    await provider.write_file("/tmp.txt", "temp")
    assert await provider.exists("/tmp.txt")
    await provider.remove("/tmp.txt")
    assert not await provider.exists("/tmp.txt")


@pytest.mark.asyncio
async def test_remove_directory_recursive(provider: SqliteFileStorageProvider):
    await provider.mkdir("/dir/sub", recursive=True)
    await provider.write_file("/dir/sub/f.txt", "x")
    await provider.remove("/dir", recursive=True)
    assert not await provider.exists("/dir")
    assert not await provider.exists("/dir/sub")
    assert not await provider.exists("/dir/sub/f.txt")


@pytest.mark.asyncio
async def test_remove_nonempty_dir_without_recursive(provider: SqliteFileStorageProvider):
    await provider.mkdir("/dir", recursive=True)
    await provider.write_file("/dir/f.txt", "x")
    with pytest.raises(FileStorageNotADirectoryError):
        await provider.remove("/dir", recursive=False)


@pytest.mark.asyncio
async def test_remove_root_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStoragePermissionError):
        await provider.remove("/")


@pytest.mark.asyncio
async def test_remove_nonexistent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.remove("/nope.txt")


# ---------------------------------------------------------------------------
# Copy & Move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy(provider: SqliteFileStorageProvider):
    await provider.write_file("/src.txt", "source")
    await provider.copy("/src.txt", "/dst.txt")
    assert await provider.read_file("/dst.txt") == "source"
    # original still exists
    assert await provider.exists("/src.txt")


@pytest.mark.asyncio
async def test_copy_directory(provider: SqliteFileStorageProvider):
    await provider.mkdir("/srcdir/sub", recursive=True)
    await provider.write_file("/srcdir/sub/f.txt", "deep")
    await provider.copy("/srcdir", "/dstdir")
    assert await provider.exists("/dstdir")
    assert await provider.exists("/dstdir/sub")
    assert await provider.read_file("/dstdir/sub/f.txt") == "deep"
    # original still intact
    assert await provider.exists("/srcdir/sub/f.txt")


@pytest.mark.asyncio
async def test_move(provider: SqliteFileStorageProvider):
    await provider.write_file("/old.txt", "data")
    await provider.move("/old.txt", "/new.txt")
    assert await provider.read_file("/new.txt") == "data"
    assert not await provider.exists("/old.txt")


@pytest.mark.asyncio
async def test_move_directory(provider: SqliteFileStorageProvider):
    await provider.mkdir("/a/b", recursive=True)
    await provider.write_file("/a/b/file.txt", "content")
    await provider.move("/a", "/c")
    assert await provider.read_file("/c/b/file.txt") == "content"
    assert not await provider.exists("/a")
    assert not await provider.exists("/a/b")


# ---------------------------------------------------------------------------
# Overwrite & edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overwrite_file(provider: SqliteFileStorageProvider):
    await provider.write_file("/f.txt", "v1")
    await provider.write_file("/f.txt", "v2")
    assert await provider.read_file("/f.txt") == "v2"


@pytest.mark.asyncio
async def test_read_nonexistent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.read_file("/nope.txt")


@pytest.mark.asyncio
async def test_read_directory_raises(provider: SqliteFileStorageProvider):
    await provider.mkdir("/dir")
    with pytest.raises(FileStorageIsADirectoryError):
        await provider.read_file("/dir")


@pytest.mark.asyncio
async def test_write_bytes(provider: SqliteFileStorageProvider):
    await provider.write_file("/bytes.txt", b"byte content")
    content = await provider.read_file("/bytes.txt")
    assert content == "byte content"


@pytest.mark.asyncio
async def test_write_creates_parent_dirs(provider: SqliteFileStorageProvider):
    await provider.write_file("/a/b/c/file.txt", "nested")
    assert await provider.exists("/a")
    assert await provider.exists("/a/b")
    assert await provider.exists("/a/b/c")
    assert await provider.read_file("/a/b/c/file.txt") == "nested"


@pytest.mark.asyncio
async def test_mkdir_non_recursive_missing_parent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.mkdir("/x/y/z", recursive=False)


@pytest.mark.asyncio
async def test_mkdir_recursive(provider: SqliteFileStorageProvider):
    await provider.mkdir("/x/y/z", recursive=True)
    assert await provider.exists("/x")
    assert await provider.exists("/x/y")
    assert await provider.exists("/x/y/z")
    st = await provider.stat("/x/y/z")
    assert st.type == "directory"


# ---------------------------------------------------------------------------
# Upload & Download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_and_download(provider: SqliteFileStorageProvider):
    await provider.upload_file("/bin.dat", b"binary data")
    stream = await provider.get_download_stream("/bin.dat")
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    assert b"".join(chunks) == b"binary data"


# ---------------------------------------------------------------------------
# Persistence (on-disk database)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_across_instances():
    """Data written by one provider instance is readable by a new instance."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # start fresh
    try:
        provider1 = SqliteFileStorageProvider(SqliteProviderOptions(db_path=path))
        await provider1.mkdir("/data", recursive=True)
        await provider1.write_file("/data/notes.txt", "persisted")

        # Create a brand new provider from the same file
        provider2 = SqliteFileStorageProvider(SqliteProviderOptions(db_path=path))
        assert await provider2.exists("/data/notes.txt")
        content = await provider2.read_file("/data/notes.txt")
        assert content == "persisted"

        entries = await provider2.list("/data")
        assert len(entries) == 1
        assert entries[0].name == "notes.txt"
    finally:
        if os.path.exists(path):
            os.unlink(path)
        # WAL/SHM files
        for suffix in ("-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                os.unlink(p)


@pytest.mark.asyncio
async def test_persistence_nested_dirs():
    """Nested directory structures survive across instances."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        p1 = SqliteFileStorageProvider(SqliteProviderOptions(db_path=path))
        await p1.mkdir("/a/b/c", recursive=True)
        await p1.write_file("/a/b/c/deep.txt", "deep")

        p2 = SqliteFileStorageProvider(SqliteProviderOptions(db_path=path))
        assert await p2.read_file("/a/b/c/deep.txt") == "deep"
        st = await p2.stat("/a/b")
        assert st.type == "directory"
    finally:
        if os.path.exists(path):
            os.unlink(path)
        for suffix in ("-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                os.unlink(p)


@pytest.mark.asyncio
async def test_provider_name(provider: SqliteFileStorageProvider):
    assert provider.name == "sqlite"


@pytest.mark.asyncio
async def test_list_nonexistent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.list("/nonexistent")


@pytest.mark.asyncio
async def test_list_file_raises(provider: SqliteFileStorageProvider):
    await provider.write_file("/f.txt", "data")
    with pytest.raises(FileStorageNotADirectoryError):
        await provider.list("/f.txt")


@pytest.mark.asyncio
async def test_stat_nonexistent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.stat("/nonexistent")


@pytest.mark.asyncio
async def test_copy_nonexistent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.copy("/nonexistent", "/dst")


@pytest.mark.asyncio
async def test_move_nonexistent_raises(provider: SqliteFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.move("/nonexistent", "/dst")


@pytest.mark.asyncio
async def test_remove_empty_directory(provider: SqliteFileStorageProvider):
    await provider.mkdir("/empty")
    await provider.remove("/empty")
    assert not await provider.exists("/empty")
