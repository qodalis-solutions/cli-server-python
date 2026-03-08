from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any, AsyncIterator

import boto3
from botocore.exceptions import ClientError

from qodalis_cli_filesystem import (
    FileEntry,
    FileStat,
    FileStorageIsADirectoryError,
    FileStorageNotADirectoryError,
    FileStorageNotFoundError,
    IFileStorageProvider,
)


@dataclass
class S3ProviderOptions:
    """Configuration for the S3 file storage provider."""

    bucket: str
    region: str | None = None
    prefix: str | None = None
    endpoint_url: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: str) -> str:
    """Normalize a filesystem path, stripping leading/trailing slashes."""
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


class S3FileStorageProvider(IFileStorageProvider):
    """File storage provider backed by an AWS S3 bucket.

    Uses ``boto3`` to interact with S3. All async methods wrap synchronous
    boto3 calls via ``asyncio.get_event_loop().run_in_executor``.

    Directories are virtual: ``mkdir`` creates a zero-byte object with a
    trailing ``/`` as a directory marker.
    """

    def __init__(self, options: S3ProviderOptions) -> None:
        self._bucket = options.bucket
        self._prefix = (options.prefix or "").strip("/")
        if self._prefix:
            self._prefix += "/"

        client_kwargs: dict[str, Any] = {}
        if options.region:
            client_kwargs["region_name"] = options.region
        if options.endpoint_url:
            client_kwargs["endpoint_url"] = options.endpoint_url
        if options.aws_access_key_id:
            client_kwargs["aws_access_key_id"] = options.aws_access_key_id
        if options.aws_secret_access_key:
            client_kwargs["aws_secret_access_key"] = options.aws_secret_access_key

        self._client = boto3.client("s3", **client_kwargs)

    def _to_key(self, norm_path: str) -> str:
        """Convert a normalized path to an S3 object key."""
        return f"{self._prefix}{norm_path}" if norm_path else self._prefix.rstrip("/")

    def _dir_key(self, norm_path: str) -> str:
        """Return the S3 key for a directory marker."""
        key = self._to_key(norm_path)
        if not key.endswith("/"):
            key += "/"
        return key

    def _dir_prefix(self, norm_path: str) -> str:
        """Return the prefix to use when listing objects under a directory."""
        if not norm_path:
            return self._prefix
        return f"{self._prefix}{norm_path}/"

    async def _run(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous boto3 call in the default executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    # -- IFileStorageProvider interface ----------------------------------------

    @property
    def name(self) -> str:
        return "s3"

    async def list(self, path: str) -> list[FileEntry]:
        norm = _normalize_path(path)
        prefix = self._dir_prefix(norm)

        response = await self._run(
            self._client.list_objects_v2,
            Bucket=self._bucket,
            Prefix=prefix,
            Delimiter="/",
        )

        # If the directory doesn't exist and there are no contents, raise
        if (
            not response.get("Contents")
            and not response.get("CommonPrefixes")
        ):
            # Check if the path is actually a file
            if norm and await self._is_file(norm):
                raise FileStorageNotADirectoryError(path)
            # For root path, return empty list; for others, raise not found
            if norm:
                raise FileStorageNotFoundError(path)
            return []

        entries: list[FileEntry] = []

        # Directories (common prefixes)
        for cp in response.get("CommonPrefixes", []):
            dir_name = cp["Prefix"][len(prefix):].rstrip("/")
            if dir_name:
                entries.append(
                    FileEntry(
                        name=dir_name,
                        type="directory",
                        size=0,
                        modified=_now_iso(),
                        permissions="drwxr-xr-x",
                    )
                )

        # Files
        for obj in response.get("Contents", []):
            key = obj["Key"]
            # Skip the directory marker itself
            if key == prefix:
                continue
            name = key[len(prefix):]
            if not name or name.endswith("/"):
                continue
            modified = obj.get("LastModified")
            mod_str = modified.isoformat() if modified else _now_iso()
            entries.append(
                FileEntry(
                    name=name,
                    type="file",
                    size=obj.get("Size", 0),
                    modified=mod_str,
                    permissions="-rw-r--r--",
                )
            )

        entries.sort(key=lambda e: e.name)
        return entries

    async def read_file(self, path: str) -> str:
        norm = _normalize_path(path)
        if not norm:
            raise FileStorageIsADirectoryError(path)

        key = self._to_key(norm)
        try:
            response = await self._run(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileStorageNotFoundError(path)
            raise

        body = response["Body"].read()
        return body.decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str | bytes) -> None:
        norm = _normalize_path(path)
        if not norm:
            raise FileStorageIsADirectoryError(path)

        key = self._to_key(norm)
        body = content.encode("utf-8") if isinstance(content, str) else content

        await self._run(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
        )

    async def stat(self, path: str) -> FileStat:
        norm = _normalize_path(path)

        if not norm:
            # Root directory
            return FileStat(
                name="/",
                type="directory",
                size=0,
                created=_now_iso(),
                modified=_now_iso(),
                permissions="drwxr-xr-x",
            )

        # Try as file first
        key = self._to_key(norm)
        try:
            head = await self._run(
                self._client.head_object,
                Bucket=self._bucket,
                Key=key,
            )
            last_modified = head.get("LastModified")
            mod_str = last_modified.isoformat() if last_modified else _now_iso()
            return FileStat(
                name=norm.rsplit("/", 1)[-1],
                type="file",
                size=head.get("ContentLength", 0),
                created=mod_str,
                modified=mod_str,
                permissions="-rw-r--r--",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "404":
                raise

        # Try as directory marker
        dir_key = self._dir_key(norm)
        try:
            head = await self._run(
                self._client.head_object,
                Bucket=self._bucket,
                Key=dir_key,
            )
            last_modified = head.get("LastModified")
            mod_str = last_modified.isoformat() if last_modified else _now_iso()
            return FileStat(
                name=norm.rsplit("/", 1)[-1],
                type="directory",
                size=0,
                created=mod_str,
                modified=mod_str,
                permissions="drwxr-xr-x",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "404":
                raise

        # Check if there are objects with this prefix (virtual directory)
        prefix = self._dir_prefix(norm)
        response = await self._run(
            self._client.list_objects_v2,
            Bucket=self._bucket,
            Prefix=prefix,
            MaxKeys=1,
        )
        if response.get("Contents") or response.get("CommonPrefixes"):
            return FileStat(
                name=norm.rsplit("/", 1)[-1],
                type="directory",
                size=0,
                created=_now_iso(),
                modified=_now_iso(),
                permissions="drwxr-xr-x",
            )

        raise FileStorageNotFoundError(path)

    async def mkdir(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if not norm:
            return  # root always exists

        dir_key = self._dir_key(norm)
        await self._run(
            self._client.put_object,
            Bucket=self._bucket,
            Key=dir_key,
            Body=b"",
        )

    async def remove(self, path: str, recursive: bool = False) -> None:
        norm = _normalize_path(path)
        if not norm:
            raise FileStorageNotFoundError(path)

        key = self._to_key(norm)

        # Check if it's a file
        if await self._is_file(norm):
            await self._run(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=key,
            )
            return

        # Try as directory
        prefix = self._dir_prefix(norm)
        response = await self._run(
            self._client.list_objects_v2,
            Bucket=self._bucket,
            Prefix=prefix,
        )

        objects = response.get("Contents", [])
        if not objects:
            raise FileStorageNotFoundError(path)

        if not recursive and len(objects) > 1:
            raise FileStorageNotADirectoryError(path)
        # If there's exactly one object and it's the dir marker, allow removal
        if (
            not recursive
            and len(objects) == 1
            and not objects[0]["Key"].endswith("/")
        ):
            raise FileStorageNotADirectoryError(path)

        # Batch delete all objects under the prefix
        delete_keys = [{"Key": obj["Key"]} for obj in objects]
        await self._run(
            self._client.delete_objects,
            Bucket=self._bucket,
            Delete={"Objects": delete_keys},
        )

    async def copy(self, src: str, dest: str) -> None:
        src_norm = _normalize_path(src)
        dest_norm = _normalize_path(dest)
        if not src_norm:
            raise FileStorageNotFoundError(src)
        if not dest_norm:
            raise FileStorageIsADirectoryError(dest)

        src_key = self._to_key(src_norm)
        dest_key = self._to_key(dest_norm)

        copy_source = {"Bucket": self._bucket, "Key": src_key}
        try:
            await self._run(
                self._client.copy_object,
                Bucket=self._bucket,
                CopySource=copy_source,
                Key=dest_key,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileStorageNotFoundError(src)
            raise

    async def move(self, src: str, dest: str) -> None:
        await self.copy(src, dest)
        src_norm = _normalize_path(src)
        src_key = self._to_key(src_norm)
        await self._run(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=src_key,
        )

    async def exists(self, path: str) -> bool:
        norm = _normalize_path(path)
        if not norm:
            return True  # root always exists

        # Check as file
        if await self._is_file(norm):
            return True

        # Check as directory marker
        dir_key = self._dir_key(norm)
        try:
            await self._run(
                self._client.head_object,
                Bucket=self._bucket,
                Key=dir_key,
            )
            return True
        except ClientError:
            pass

        # Check if any objects exist under this prefix (virtual dir)
        prefix = self._dir_prefix(norm)
        response = await self._run(
            self._client.list_objects_v2,
            Bucket=self._bucket,
            Prefix=prefix,
            MaxKeys=1,
        )
        return bool(response.get("Contents") or response.get("CommonPrefixes"))

    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]:
        norm = _normalize_path(path)
        if not norm:
            raise FileStorageIsADirectoryError(path)

        key = self._to_key(norm)
        try:
            response = await self._run(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileStorageNotFoundError(path)
            raise

        body = response["Body"]

        async def _stream() -> AsyncIterator[bytes]:
            chunk_size = 8192
            while True:
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, body.read, chunk_size
                )
                if not chunk:
                    break
                yield chunk

        return _stream()

    async def upload_file(self, path: str, content: bytes) -> None:
        await self.write_file(path, content)

    # -- Private helpers ------------------------------------------------------

    async def _is_file(self, norm_path: str) -> bool:
        """Check if a normalized path corresponds to a file object."""
        key = self._to_key(norm_path)
        try:
            await self._run(
                self._client.head_object,
                Bucket=self._bucket,
                Key=key,
            )
            return True
        except ClientError:
            return False
