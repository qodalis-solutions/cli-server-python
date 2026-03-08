"""Tests for the InMemoryFileStorageProvider."""

from __future__ import annotations

import pytest

from plugins.filesystem import (
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
)
from plugins.filesystem.providers import InMemoryFileStorageProvider


@pytest.fixture()
def provider() -> InMemoryFileStorageProvider:
    return InMemoryFileStorageProvider()


class TestMkdir:
    async def test_mkdir_creates_directory(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/docs", recursive=False)
        assert await provider.exists("/docs")
        info = await provider.stat("/docs")
        assert info.type == "directory"

    async def test_mkdir_recursive(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/a/b/c", recursive=True)
        assert await provider.exists("/a")
        assert await provider.exists("/a/b")
        assert await provider.exists("/a/b/c")

    async def test_mkdir_non_recursive_fails_without_parent(
        self, provider: InMemoryFileStorageProvider
    ) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.mkdir("/x/y/z", recursive=False)


class TestWriteAndRead:
    async def test_write_and_read_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/docs", recursive=True)
        await provider.write_file("/docs/hello.txt", "Hello, World!")
        content = await provider.read_file("/docs/hello.txt")
        assert content == "Hello, World!"

    async def test_write_creates_parents(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/a/b/file.txt", "data")
        content = await provider.read_file("/a/b/file.txt")
        assert content == "data"

    async def test_write_bytes(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/binary.bin", b"\x00\x01\x02")
        info = await provider.stat("/binary.bin")
        assert info.size == 3

    async def test_overwrite_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/f.txt", "old")
        await provider.write_file("/f.txt", "new")
        assert await provider.read_file("/f.txt") == "new"

    async def test_read_nonexistent_raises(self, provider: InMemoryFileStorageProvider) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.read_file("/nope.txt")

    async def test_read_directory_raises(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/dir")
        with pytest.raises(FileStorageIsADirectoryError):
            await provider.read_file("/dir")

    async def test_write_over_directory_raises(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/dir")
        with pytest.raises(FileStorageIsADirectoryError):
            await provider.write_file("/dir", "data")


class TestList:
    async def test_list_empty_root(self, provider: InMemoryFileStorageProvider) -> None:
        entries = await provider.list("/")
        assert entries == []

    async def test_list_directory(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/a.txt", "a")
        await provider.write_file("/b.txt", "b")
        await provider.mkdir("/subdir")
        entries = await provider.list("/")
        names = [e.name for e in entries]
        assert names == ["a.txt", "b.txt", "subdir"]

    async def test_list_nonexistent_raises(self, provider: InMemoryFileStorageProvider) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.list("/nonexistent")

    async def test_list_file_raises(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/f.txt", "data")
        with pytest.raises(FileStorageNotADirectoryError):
            await provider.list("/f.txt")


class TestStat:
    async def test_stat_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/hello.txt", "hello")
        info = await provider.stat("/hello.txt")
        assert info.name == "hello.txt"
        assert info.type == "file"
        assert info.size == 5

    async def test_stat_directory(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/mydir")
        info = await provider.stat("/mydir")
        assert info.name == "mydir"
        assert info.type == "directory"

    async def test_stat_nonexistent_raises(self, provider: InMemoryFileStorageProvider) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.stat("/nope")


class TestRemove:
    async def test_remove_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/f.txt", "data")
        await provider.remove("/f.txt")
        assert not await provider.exists("/f.txt")

    async def test_remove_empty_dir(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/d")
        await provider.remove("/d")
        assert not await provider.exists("/d")

    async def test_remove_non_empty_dir_non_recursive_raises(
        self, provider: InMemoryFileStorageProvider
    ) -> None:
        await provider.mkdir("/d")
        await provider.write_file("/d/f.txt", "x")
        with pytest.raises(FileStorageNotADirectoryError):
            await provider.remove("/d", recursive=False)

    async def test_remove_non_empty_dir_recursive(
        self, provider: InMemoryFileStorageProvider
    ) -> None:
        await provider.mkdir("/d")
        await provider.write_file("/d/f.txt", "x")
        await provider.remove("/d", recursive=True)
        assert not await provider.exists("/d")

    async def test_remove_nonexistent_raises(self, provider: InMemoryFileStorageProvider) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.remove("/nope")


class TestCopy:
    async def test_copy_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/src.txt", "content")
        await provider.copy("/src.txt", "/dest.txt")
        assert await provider.read_file("/dest.txt") == "content"
        # Original still exists
        assert await provider.exists("/src.txt")

    async def test_copy_directory(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/src")
        await provider.write_file("/src/a.txt", "a")
        await provider.copy("/src", "/dest")
        assert await provider.read_file("/dest/a.txt") == "a"

    async def test_copy_nonexistent_raises(self, provider: InMemoryFileStorageProvider) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.copy("/nope", "/dest")


class TestMove:
    async def test_move_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/src.txt", "content")
        await provider.move("/src.txt", "/dest.txt")
        assert await provider.read_file("/dest.txt") == "content"
        assert not await provider.exists("/src.txt")

    async def test_move_directory(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/src")
        await provider.write_file("/src/a.txt", "a")
        await provider.move("/src", "/dest")
        assert await provider.read_file("/dest/a.txt") == "a"
        assert not await provider.exists("/src")

    async def test_move_nonexistent_raises(self, provider: InMemoryFileStorageProvider) -> None:
        with pytest.raises(FileStorageNotFoundError):
            await provider.move("/nope", "/dest")


class TestExists:
    async def test_exists_returns_false_for_missing(
        self, provider: InMemoryFileStorageProvider
    ) -> None:
        assert not await provider.exists("/nope")

    async def test_exists_returns_true_for_file(
        self, provider: InMemoryFileStorageProvider
    ) -> None:
        await provider.write_file("/f.txt", "x")
        assert await provider.exists("/f.txt")

    async def test_exists_root(self, provider: InMemoryFileStorageProvider) -> None:
        assert await provider.exists("/")


class TestDownloadStream:
    async def test_get_download_stream(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.write_file("/f.txt", "hello")
        stream = await provider.get_download_stream("/f.txt")
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        assert b"".join(chunks) == b"hello"

    async def test_download_directory_raises(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.mkdir("/d")
        with pytest.raises(FileStorageIsADirectoryError):
            await provider.get_download_stream("/d")


class TestUploadFile:
    async def test_upload_file(self, provider: InMemoryFileStorageProvider) -> None:
        await provider.upload_file("/up.bin", b"binary data")
        info = await provider.stat("/up.bin")
        assert info.size == len(b"binary data")


class TestProviderName:
    def test_name(self, provider: InMemoryFileStorageProvider) -> None:
        assert provider.name == "in-memory"
