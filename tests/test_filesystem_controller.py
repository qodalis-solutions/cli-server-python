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
    app.include_router(router, prefix="/api/cli/fs")
    return TestClient(app)


class TestListEndpoint:
    def test_list_empty_root(self, client: TestClient) -> None:
        resp = client.get("/api/cli/fs/ls", params={"path": "/"})
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    def test_list_with_files(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/a.txt", "a"))
        loop.run_until_complete(provider.mkdir("/subdir"))
        loop.close()

        resp = client.get("/api/cli/fs/ls", params={"path": "/"})
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        names = [e["name"] for e in entries]
        assert "a.txt" in names
        assert "subdir" in names

    def test_list_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/cli/fs/ls", params={"path": "/nope"})
        assert resp.status_code == 404

    def test_list_file_returns_400(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/f.txt", "data"))
        loop.close()

        resp = client.get("/api/cli/fs/ls", params={"path": "/f.txt"})
        assert resp.status_code == 400


class TestCatEndpoint:
    def test_read_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/hello.txt", "Hello!"))
        loop.close()

        resp = client.get("/api/cli/fs/cat", params={"path": "/hello.txt"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "Hello!"

    def test_read_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/cli/fs/cat", params={"path": "/nope.txt"})
        assert resp.status_code == 404

    def test_read_directory_returns_400(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.mkdir("/dir"))
        loop.close()

        resp = client.get("/api/cli/fs/cat", params={"path": "/dir"})
        assert resp.status_code == 400


class TestStatEndpoint:
    def test_stat_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/f.txt", "data"))
        loop.close()

        resp = client.get("/api/cli/fs/stat", params={"path": "/f.txt"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "f.txt"
        assert body["type"] == "file"
        assert body["size"] == 4

    def test_stat_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/cli/fs/stat", params={"path": "/nope"})
        assert resp.status_code == 404


class TestMkdirEndpoint:
    def test_mkdir(self, client: TestClient) -> None:
        resp = client.post("/api/cli/fs/mkdir", json={"path": "/newdir"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

        # Verify it exists
        resp = client.get("/api/cli/fs/stat", params={"path": "/newdir"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "directory"

    def test_mkdir_missing_path_returns_400(self, client: TestClient) -> None:
        resp = client.post("/api/cli/fs/mkdir", json={})
        assert resp.status_code == 400


class TestRmEndpoint:
    def test_rm_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/f.txt", "data"))
        loop.close()

        resp = client.delete("/api/cli/fs/rm", params={"path": "/f.txt"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify it's gone
        resp = client.get("/api/cli/fs/stat", params={"path": "/f.txt"})
        assert resp.status_code == 404

    def test_rm_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/api/cli/fs/rm", params={"path": "/nope"})
        assert resp.status_code == 404


class TestUploadEndpoint:
    def test_upload_file(self, client: TestClient) -> None:
        resp = client.post(
            "/api/cli/fs/upload",
            data={"path": "/uploaded.txt"},
            files={"file": ("uploaded.txt", b"file content", "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "uploaded"

        # Verify content
        resp = client.get("/api/cli/fs/cat", params={"path": "/uploaded.txt"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "file content"


class TestDownloadEndpoint:
    def test_download_file(self, client: TestClient, provider: InMemoryFileStorageProvider) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(provider.write_file("/dl.txt", "download me"))
        loop.close()

        resp = client.get("/api/cli/fs/download", params={"path": "/dl.txt"})
        assert resp.status_code == 200
        assert resp.content == b"download me"

    def test_download_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/cli/fs/download", params={"path": "/nope"})
        assert resp.status_code == 404
