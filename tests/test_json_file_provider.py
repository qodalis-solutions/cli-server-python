from __future__ import annotations

import importlib
import os
import tempfile

import pytest
import pytest_asyncio

from plugins.filesystem.errors import (
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
)

# The plugin directory contains a hyphen, so we use importlib.
_mod = importlib.import_module("plugins.filesystem-json")
JsonFileProviderOptions = _mod.JsonFileProviderOptions
JsonFileStorageProvider = _mod.JsonFileStorageProvider


@pytest_asyncio.fixture
async def tmp_json_path():
    """Yield a temporary file path and clean up after the test."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start with no file
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest_asyncio.fixture
async def provider(tmp_json_path: str):
    """Create a fresh JsonFileStorageProvider."""
    return JsonFileStorageProvider(JsonFileProviderOptions(file_path=tmp_json_path))


@pytest.mark.asyncio
async def test_write_and_read_file(provider: JsonFileStorageProvider):
    await provider.write_file("/hello.txt", "Hello, world!")
    content = await provider.read_file("/hello.txt")
    assert content == "Hello, world!"


@pytest.mark.asyncio
async def test_exists(provider: JsonFileStorageProvider):
    assert not await provider.exists("/foo.txt")
    await provider.write_file("/foo.txt", "data")
    assert await provider.exists("/foo.txt")


@pytest.mark.asyncio
async def test_mkdir_and_list(provider: JsonFileStorageProvider):
    await provider.mkdir("/docs", recursive=True)
    await provider.write_file("/docs/readme.txt", "Read me")
    entries = await provider.list("/docs")
    assert len(entries) == 1
    assert entries[0].name == "readme.txt"
    assert entries[0].type == "file"


@pytest.mark.asyncio
async def test_list_root(provider: JsonFileStorageProvider):
    await provider.write_file("/a.txt", "a")
    await provider.write_file("/b.txt", "b")
    entries = await provider.list("/")
    names = [e.name for e in entries]
    assert "a.txt" in names
    assert "b.txt" in names


@pytest.mark.asyncio
async def test_stat(provider: JsonFileStorageProvider):
    await provider.write_file("/file.txt", "content")
    st = await provider.stat("/file.txt")
    assert st.name == "file.txt"
    assert st.type == "file"
    assert st.size == len("content".encode("utf-8"))
    assert st.permissions == "-rw-r--r--"


@pytest.mark.asyncio
async def test_remove_file(provider: JsonFileStorageProvider):
    await provider.write_file("/tmp.txt", "temp")
    assert await provider.exists("/tmp.txt")
    await provider.remove("/tmp.txt")
    assert not await provider.exists("/tmp.txt")


@pytest.mark.asyncio
async def test_remove_directory_recursive(provider: JsonFileStorageProvider):
    await provider.mkdir("/dir/sub", recursive=True)
    await provider.write_file("/dir/sub/f.txt", "x")
    await provider.remove("/dir", recursive=True)
    assert not await provider.exists("/dir")


@pytest.mark.asyncio
async def test_remove_nonempty_dir_without_recursive(provider: JsonFileStorageProvider):
    await provider.mkdir("/dir", recursive=True)
    await provider.write_file("/dir/f.txt", "x")
    with pytest.raises(FileStorageNotADirectoryError):
        await provider.remove("/dir", recursive=False)


@pytest.mark.asyncio
async def test_copy(provider: JsonFileStorageProvider):
    await provider.write_file("/src.txt", "source")
    await provider.copy("/src.txt", "/dst.txt")
    assert await provider.read_file("/dst.txt") == "source"
    # original still exists
    assert await provider.exists("/src.txt")


@pytest.mark.asyncio
async def test_move(provider: JsonFileStorageProvider):
    await provider.write_file("/old.txt", "data")
    await provider.move("/old.txt", "/new.txt")
    assert await provider.read_file("/new.txt") == "data"
    assert not await provider.exists("/old.txt")


@pytest.mark.asyncio
async def test_overwrite_file(provider: JsonFileStorageProvider):
    await provider.write_file("/f.txt", "v1")
    await provider.write_file("/f.txt", "v2")
    assert await provider.read_file("/f.txt") == "v2"


@pytest.mark.asyncio
async def test_read_nonexistent_raises(provider: JsonFileStorageProvider):
    with pytest.raises(FileStorageNotFoundError):
        await provider.read_file("/nope.txt")


@pytest.mark.asyncio
async def test_read_directory_raises(provider: JsonFileStorageProvider):
    await provider.mkdir("/dir")
    with pytest.raises(FileStorageIsADirectoryError):
        await provider.read_file("/dir")


@pytest.mark.asyncio
async def test_upload_and_download(provider: JsonFileStorageProvider):
    await provider.upload_file("/bin.dat", b"binary data")
    stream = await provider.get_download_stream("/bin.dat")
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    assert b"".join(chunks) == b"binary data"


@pytest.mark.asyncio
async def test_persistence_across_instances(tmp_json_path: str):
    """Data written by one provider instance is readable by a new instance."""
    provider1 = JsonFileStorageProvider(JsonFileProviderOptions(file_path=tmp_json_path))
    await provider1.mkdir("/data", recursive=True)
    await provider1.write_file("/data/notes.txt", "persisted")

    # Create a brand new provider from the same file
    provider2 = JsonFileStorageProvider(JsonFileProviderOptions(file_path=tmp_json_path))
    assert await provider2.exists("/data/notes.txt")
    content = await provider2.read_file("/data/notes.txt")
    assert content == "persisted"

    entries = await provider2.list("/data")
    assert len(entries) == 1
    assert entries[0].name == "notes.txt"


@pytest.mark.asyncio
async def test_persistence_nested_dirs(tmp_json_path: str):
    """Nested directory structures survive serialization round-trip."""
    p1 = JsonFileStorageProvider(JsonFileProviderOptions(file_path=tmp_json_path))
    await p1.mkdir("/a/b/c", recursive=True)
    await p1.write_file("/a/b/c/deep.txt", "deep")

    p2 = JsonFileStorageProvider(JsonFileProviderOptions(file_path=tmp_json_path))
    assert await p2.read_file("/a/b/c/deep.txt") == "deep"
    st = await p2.stat("/a/b")
    assert st.type == "directory"


@pytest.mark.asyncio
async def test_write_bytes(provider: JsonFileStorageProvider):
    await provider.write_file("/bytes.txt", b"byte content")
    content = await provider.read_file("/bytes.txt")
    assert content == "byte content"
