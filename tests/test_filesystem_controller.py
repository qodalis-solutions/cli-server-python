"""Integration tests for the filesystem controller with InMemoryProvider."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.filesystem.providers import InMemoryFileStorageProvider
from qodalis_cli.controllers.filesystem_controller import create_filesystem_router


@pytest.fixture()
def provider() -> InMemoryFileStorageProvider:
    return InMemoryFileStorageProvider()


@pytest.fixture()
def client(provider: InMemoryFileStorageProvider) -> TestClient:
    app = FastAPI()
    router = create_filesystem_router(provider)
    app.include_router(router, prefix="/api/qcli/fs")
    return TestClient(app)


class TestListEndpoint:
    def test_list_empty_root(self, client: TestClient) -> None:
        resp = client.get("/api/qcli/fs/ls", params={"path": "/"})
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    def test_list_with_files(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/a.txt", "a"))
        loop.run_until_complete(provider.mkdir("/subdir"))
        loop.close()

        resp = client.get("/api/qcli/fs/ls", params={"path": "/"})
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        names = [e["name"] for e in entries]
        assert "a.txt" in names
        assert "subdir" in names

    def test_list_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/qcli/fs/ls", params={"path": "/nope"})
        assert resp.status_code == 404

    def test_list_file_returns_400(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/f.txt", "data"))
        loop.close()

        resp = client.get("/api/qcli/fs/ls", params={"path": "/f.txt"})
        assert resp.status_code == 400


class TestCatEndpoint:
    def test_read_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/hello.txt", "Hello!"))
        loop.close()

        resp = client.get("/api/qcli/fs/cat", params={"path": "/hello.txt"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "Hello!"

    def test_read_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/qcli/fs/cat", params={"path": "/nope.txt"})
        assert resp.status_code == 404

    def test_read_directory_returns_400(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.mkdir("/dir"))
        loop.close()

        resp = client.get("/api/qcli/fs/cat", params={"path": "/dir"})
        assert resp.status_code == 400


class TestStatEndpoint:
    def test_stat_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/f.txt", "data"))
        loop.close()

        resp = client.get("/api/qcli/fs/stat", params={"path": "/f.txt"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "f.txt"
        assert body["type"] == "file"
        assert body["size"] == 4

    def test_stat_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/qcli/fs/stat", params={"path": "/nope"})
        assert resp.status_code == 404


class TestMkdirEndpoint:
    def test_mkdir(self, client: TestClient) -> None:
        resp = client.post("/api/qcli/fs/mkdir", json={"path": "/newdir"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

        # Verify it exists
        resp = client.get("/api/qcli/fs/stat", params={"path": "/newdir"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "directory"

    def test_mkdir_missing_path_returns_400(self, client: TestClient) -> None:
        resp = client.post("/api/qcli/fs/mkdir", json={})
        assert resp.status_code == 400


class TestRmEndpoint:
    def test_rm_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/f.txt", "data"))
        loop.close()

        resp = client.delete("/api/qcli/fs/rm", params={"path": "/f.txt"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify it's gone
        resp = client.get("/api/qcli/fs/stat", params={"path": "/f.txt"})
        assert resp.status_code == 404

    def test_rm_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/api/qcli/fs/rm", params={"path": "/nope"})
        assert resp.status_code == 404


class TestUploadEndpoint:
    def test_upload_file(self, client: TestClient) -> None:
        resp = client.post(
            "/api/qcli/fs/upload",
            data={"path": "/uploaded.txt"},
            files={"file": ("uploaded.txt", b"file content", "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "uploaded"

        # Verify content
        resp = client.get("/api/qcli/fs/cat", params={"path": "/uploaded.txt"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "file content"


class TestUploadLimit:
    """``max_upload_bytes``: oversize uploads get 413 (JSON), not 500, and
    are never buffered beyond the cap."""

    @staticmethod
    def _client(provider_or_validator, max_upload_bytes: int) -> TestClient:
        app = FastAPI()
        app.include_router(
            create_filesystem_router(provider_or_validator, max_upload_bytes=max_upload_bytes),
            prefix="/api/qcli/fs",
        )
        return TestClient(app)

    @staticmethod
    def _upload(client: TestClient, content: bytes, path: str = "/big.bin"):
        return client.post(
            "/api/qcli/fs/upload",
            data={"path": path},
            files={"file": ("big.bin", content, "application/octet-stream")},
        )

    def test_default_limit_is_100_mib(self) -> None:
        from qodalis_cli.filesystem import FileSystemOptions

        assert FileSystemOptions().max_upload_bytes == 104857600

    def test_file_at_limit_is_accepted(self, provider: InMemoryFileStorageProvider) -> None:
        client = self._client(provider, 10)
        resp = self._upload(client, b"0123456789")
        assert resp.status_code == 200
        assert client.get("/api/qcli/fs/cat", params={"path": "/big.bin"}).json()["content"] == "0123456789"

    def test_file_over_limit_is_413(self, provider: InMemoryFileStorageProvider) -> None:
        client = self._client(provider, 10)
        resp = self._upload(client, b"0123456789X")
        assert resp.status_code == 413
        assert resp.headers["content-type"] == "application/json"
        assert resp.json() == {"detail": "File exceeds the maximum upload size of 10 bytes"}
        assert client.get("/api/qcli/fs/stat", params={"path": "/big.bin"}).status_code == 404

    def test_large_body_rejected_by_content_length(self, provider: InMemoryFileStorageProvider) -> None:
        client = self._client(provider, 10)
        resp = self._upload(client, b"x" * (2 * 1024 * 1024))
        assert resp.status_code == 413

    def test_chunked_body_stops_at_cap(self, provider: InMemoryFileStorageProvider) -> None:
        """Without Content-Length the body is cut off once it passes the cap
        (plus multipart overhead): the rest is never read."""
        boundary = "qcliboundary"
        head = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n/big.bin\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()

        def body():
            yield head
            for _ in range(64):  # 64 x 64 KiB = 4 MiB
                yield b"x" * 65536
            yield f"\r\n--{boundary}--\r\n".encode()

        client = self._client(provider, 10)
        resp = client.post(
            "/api/qcli/fs/upload",
            content=body(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert resp.status_code == 413
        assert client.get("/api/qcli/fs/stat", params={"path": "/big.bin"}).status_code == 404

    def test_body_reader_stops_after_limit(self) -> None:
        import asyncio

        from qodalis_cli.controllers.filesystem_controller import _limited_receive, _UploadTooLarge

        pulled = 0

        async def receive():
            nonlocal pulled
            pulled += 1
            return {"type": "http.request", "body": b"x" * 100, "more_body": True}

        async def run():
            limited = _limited_receive(receive, 250)
            await limited()
            await limited()
            with pytest.raises(_UploadTooLarge):
                await limited()

        asyncio.run(run())
        assert pulled == 3  # nothing read past the message that crossed the cap

    def test_legacy_router_limit(self, tmp_path) -> None:
        from qodalis_cli.filesystem import FileSystemPathValidator

        validator = FileSystemPathValidator([str(tmp_path)])
        client = self._client(validator, 10)
        target = str(tmp_path / "big.bin")
        assert self._upload(client, b"0123456789X", target).status_code == 413
        assert not (tmp_path / "big.bin").exists()
        assert self._upload(client, b"0123456789", target).status_code == 200
        assert (tmp_path / "big.bin").read_bytes() == b"0123456789"

    def test_server_option(self, tmp_path) -> None:
        from qodalis_cli import CliServerOptions, create_cli_server
        from qodalis_cli.filesystem import FileSystemOptions

        result = create_cli_server(CliServerOptions(configure=lambda b: b.add_filesystem(
            FileSystemOptions(allowed_paths=[str(tmp_path)], max_upload_bytes=10),
        )))
        client = TestClient(result.app)
        assert self._upload(client, b"0123456789X", str(tmp_path / "a.bin")).status_code == 413
        assert self._upload(client, b"0123456789", str(tmp_path / "a.bin")).status_code == 200

    def test_missing_fields_is_422(self, provider: InMemoryFileStorageProvider) -> None:
        client = self._client(provider, 10)
        resp = client.post("/api/qcli/fs/upload", data={"path": "/x"})
        assert resp.status_code == 422


class TestDownloadEndpoint:
    def test_download_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/dl.txt", "download me"))
        loop.close()

        resp = client.get("/api/qcli/fs/download", params={"path": "/dl.txt"})
        assert resp.status_code == 200
        assert resp.content == b"download me"

    def test_download_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/qcli/fs/download", params={"path": "/nope"})
        assert resp.status_code == 404
