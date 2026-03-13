from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from qodalis_cli_filesystem import (
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
)

import qodalis_cli_filesystem_s3.s3_provider as _s3_mod
from qodalis_cli_filesystem_s3 import S3ProviderOptions, S3FileStorageProvider


BUCKET = "test-bucket"
NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_provider(
    prefix: str | None = None,
    mock_client: MagicMock | None = None,
) -> tuple[S3FileStorageProvider, MagicMock]:
    """Create a provider with a mocked boto3 client."""
    client = mock_client or MagicMock()
    with patch.object(_s3_mod, "boto3") as mock_boto3:
        mock_boto3.client.return_value = client
        opts = S3ProviderOptions(bucket=BUCKET, prefix=prefix)
        provider = S3FileStorageProvider(opts)
    return provider, client


def _client_error(code: str = "404"):
    """Build a botocore ClientError with the given error code."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "Not Found"}},
        "HeadObject",
    )


# -- Path-to-key mapping -----------------------------------------------------


class TestPathToKey:
    def test_no_prefix(self):
        provider, _ = _make_provider(prefix=None)
        assert provider._to_key("home/user/file.txt") == "home/user/file.txt"

    def test_with_prefix(self):
        provider, _ = _make_provider(prefix="cli-files/")
        assert provider._to_key("home/user/file.txt") == "cli-files/home/user/file.txt"

    def test_prefix_without_trailing_slash(self):
        provider, _ = _make_provider(prefix="data")
        assert provider._to_key("file.txt") == "data/file.txt"

    def test_dir_key_no_prefix(self):
        provider, _ = _make_provider(prefix=None)
        assert provider._dir_key("mydir") == "mydir/"

    def test_dir_key_with_prefix(self):
        provider, _ = _make_provider(prefix="cli-files")
        assert provider._dir_key("mydir") == "cli-files/mydir/"

    def test_dir_prefix_root(self):
        provider, _ = _make_provider(prefix="data")
        assert provider._dir_prefix("") == "data/"

    def test_dir_prefix_subdir(self):
        provider, _ = _make_provider(prefix="data")
        assert provider._dir_prefix("sub/dir") == "data/sub/dir/"


# -- Provider name ------------------------------------------------------------


class TestName:
    def test_name(self):
        provider, _ = _make_provider()
        assert provider.name == "s3"


# -- write_file + read_file ---------------------------------------------------


@pytest.mark.asyncio
async def test_write_file():
    provider, client = _make_provider(prefix="pfx")
    await provider.write_file("/hello.txt", "Hello!")
    client.put_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="pfx/hello.txt",
        Body=b"Hello!",
    )


@pytest.mark.asyncio
async def test_write_file_bytes():
    provider, client = _make_provider()
    await provider.write_file("/bin.dat", b"\x00\x01")
    client.put_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="bin.dat",
        Body=b"\x00\x01",
    )


@pytest.mark.asyncio
async def test_write_file_root_raises():
    provider, _ = _make_provider()
    with pytest.raises(FileStorageIsADirectoryError):
        await provider.write_file("/", "data")


@pytest.mark.asyncio
async def test_read_file():
    provider, client = _make_provider(prefix="pfx")
    body_stream = MagicMock()
    body_stream.read.return_value = b"contents"
    client.get_object.return_value = {"Body": body_stream}

    result = await provider.read_file("/hello.txt")
    assert result == "contents"
    client.get_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="pfx/hello.txt",
    )


@pytest.mark.asyncio
async def test_read_file_not_found():
    provider, client = _make_provider()
    client.get_object.side_effect = _client_error("NoSuchKey")
    with pytest.raises(FileStorageNotFoundError):
        await provider.read_file("/missing.txt")


@pytest.mark.asyncio
async def test_read_file_root_raises():
    provider, _ = _make_provider()
    with pytest.raises(FileStorageIsADirectoryError):
        await provider.read_file("/")


# -- exists -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_root():
    provider, _ = _make_provider()
    assert await provider.exists("/") is True


@pytest.mark.asyncio
async def test_exists_file():
    provider, client = _make_provider()
    client.head_object.return_value = {"ContentLength": 10}
    assert await provider.exists("/file.txt") is True


@pytest.mark.asyncio
async def test_exists_missing():
    provider, client = _make_provider()
    client.head_object.side_effect = _client_error("404")
    client.list_objects_v2.return_value = {}
    assert await provider.exists("/nope.txt") is False


# -- mkdir --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mkdir():
    provider, client = _make_provider(prefix="pfx")
    await provider.mkdir("/newdir")
    client.put_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="pfx/newdir/",
        Body=b"",
    )


@pytest.mark.asyncio
async def test_mkdir_root_noop():
    provider, client = _make_provider()
    await provider.mkdir("/")
    client.put_object.assert_not_called()


# -- stat ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stat_file():
    provider, client = _make_provider()
    client.head_object.return_value = {
        "ContentLength": 42,
        "LastModified": NOW,
    }
    result = await provider.stat("/myfile.txt")
    assert result.name == "myfile.txt"
    assert result.type == "file"
    assert result.size == 42


@pytest.mark.asyncio
async def test_stat_root():
    provider, _ = _make_provider()
    result = await provider.stat("/")
    assert result.type == "directory"


@pytest.mark.asyncio
async def test_stat_not_found():
    provider, client = _make_provider()
    client.head_object.side_effect = _client_error("404")
    client.list_objects_v2.return_value = {}
    with pytest.raises(FileStorageNotFoundError):
        await provider.stat("/ghost")


# -- list ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_directory():
    provider, client = _make_provider(prefix="pfx")
    client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "pfx/dir/", "Size": 0, "LastModified": NOW},
            {"Key": "pfx/dir/file.txt", "Size": 100, "LastModified": NOW},
        ],
        "CommonPrefixes": [
            {"Prefix": "pfx/dir/subdir/"},
        ],
    }
    entries = await provider.list("/dir")
    names = [e.name for e in entries]
    assert "file.txt" in names
    assert "subdir" in names


@pytest.mark.asyncio
async def test_list_not_found():
    provider, client = _make_provider()
    client.list_objects_v2.return_value = {}
    # Make sure _is_file returns False
    client.head_object.side_effect = _client_error("404")
    with pytest.raises(FileStorageNotFoundError):
        await provider.list("/missing")


# -- remove -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_file():
    provider, client = _make_provider()
    # _is_file returns True
    client.head_object.return_value = {"ContentLength": 5}
    await provider.remove("/file.txt")
    client.delete_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="file.txt",
    )


@pytest.mark.asyncio
async def test_remove_directory_recursive():
    provider, client = _make_provider()
    # _is_file returns False
    client.head_object.side_effect = _client_error("404")
    client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "dir/", "Size": 0},
            {"Key": "dir/a.txt", "Size": 10},
            {"Key": "dir/b.txt", "Size": 20},
        ],
    }
    await provider.remove("/dir", recursive=True)
    client.delete_objects.assert_called_once()
    delete_arg = client.delete_objects.call_args[1]["Delete"]
    assert len(delete_arg["Objects"]) == 3


@pytest.mark.asyncio
async def test_remove_not_found():
    provider, client = _make_provider()
    client.head_object.side_effect = _client_error("404")
    client.list_objects_v2.return_value = {}
    with pytest.raises(FileStorageNotFoundError):
        await provider.remove("/nope")


# -- copy / move --------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy():
    provider, client = _make_provider(prefix="pfx")
    await provider.copy("/a.txt", "/b.txt")
    client.copy_object.assert_called_once_with(
        Bucket=BUCKET,
        CopySource={"Bucket": BUCKET, "Key": "pfx/a.txt"},
        Key="pfx/b.txt",
    )


@pytest.mark.asyncio
async def test_copy_not_found():
    provider, client = _make_provider()
    client.copy_object.side_effect = _client_error("NoSuchKey")
    with pytest.raises(FileStorageNotFoundError):
        await provider.copy("/missing.txt", "/dest.txt")


@pytest.mark.asyncio
async def test_move():
    provider, client = _make_provider(prefix="pfx")
    await provider.move("/a.txt", "/b.txt")
    client.copy_object.assert_called_once()
    client.delete_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="pfx/a.txt",
    )


# -- upload_file --------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file():
    provider, client = _make_provider()
    await provider.upload_file("/data.bin", b"\xff\xfe")
    client.put_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="data.bin",
        Body=b"\xff\xfe",
    )


# -- get_download_stream ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_download_stream():
    provider, client = _make_provider()
    body_mock = MagicMock()
    body_mock.read.side_effect = [b"chunk1", b"chunk2", b""]
    client.get_object.return_value = {"Body": body_mock}

    stream = await provider.get_download_stream("/file.txt")
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    assert chunks == [b"chunk1", b"chunk2"]


@pytest.mark.asyncio
async def test_get_download_stream_not_found():
    provider, client = _make_provider()
    client.get_object.side_effect = _client_error("NoSuchKey")
    with pytest.raises(FileStorageNotFoundError):
        await provider.get_download_stream("/missing.txt")
