"""An allowed root of the OS provider cannot be removed, moved or replaced."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.controllers.filesystem_controller import create_filesystem_router
from qodalis_cli.filesystem import FileSystemOptions, FileSystemPathValidator
from qodalis_cli_filesystem import FileStoragePermissionError
from qodalis_cli_filesystem.providers.os_provider import OsFileStorageProvider, OsProviderOptions


@pytest.fixture()
def root(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.txt").write_text("a")
    (share / "sub").mkdir()
    return share


@pytest.fixture()
def provider(root) -> OsFileStorageProvider:
    return OsFileStorageProvider(OsProviderOptions(allowed_paths=[str(root)]))


@pytest.mark.parametrize("suffix", ["", "/", "/.", "/sub/.."])
async def test_remove_root_is_denied(provider, root, suffix) -> None:
    with pytest.raises(FileStoragePermissionError):
        await provider.remove(f"{root}{suffix}", recursive=True)
    assert (root / "a.txt").exists()


async def test_remove_root_through_symlink_is_denied(provider, root) -> None:
    os.symlink(root, root / "link")
    with pytest.raises(FileStoragePermissionError):
        await provider.remove(str(root / "link"), recursive=True)
    assert (root / "a.txt").exists()


async def test_move_root_is_denied(provider, root) -> None:
    with pytest.raises(FileStoragePermissionError):
        await provider.move(str(root), str(root / "sub" / "moved"))
    assert (root / "a.txt").exists()


async def test_move_onto_root_is_denied(tmp_path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "f.txt").write_text("f")
    provider = OsFileStorageProvider(OsProviderOptions(allowed_paths=[str(first), str(second)]))
    with pytest.raises(FileStoragePermissionError):
        await provider.move(str(first / "f.txt"), str(second))
    assert (first / "f.txt").exists()


async def test_children_can_still_be_removed_and_moved(provider, root) -> None:
    await provider.move(str(root / "a.txt"), str(root / "sub" / "b.txt"))
    assert (root / "sub" / "b.txt").read_text() == "a"
    await provider.remove(str(root / "sub"), recursive=True)
    assert not (root / "sub").exists()
    assert root.exists()


def test_rm_root_over_http_is_403(root) -> None:
    result = create_cli_server(CliServerOptions(
        configure=lambda b: b.add_filesystem(FileSystemOptions(allowed_paths=[str(root)])),
    ))
    client = TestClient(result.app)
    resp = client.delete("/api/qcli/fs/rm", params={"path": str(root)})
    assert resp.status_code == 403
    assert (root / "a.txt").exists()
    assert client.delete("/api/qcli/fs/rm", params={"path": str(root / "a.txt")}).status_code == 200


def test_legacy_router_rm_root_is_403(root) -> None:
    app = FastAPI()
    app.include_router(create_filesystem_router(FileSystemPathValidator([str(root)])), prefix="/api/qcli/fs")
    client = TestClient(app)
    resp = client.delete("/api/qcli/fs/rm", params={"path": f"{root}/"})
    assert resp.status_code == 403
    assert (root / "a.txt").exists()
    assert client.delete("/api/qcli/fs/rm", params={"path": str(root / "sub")}).status_code == 200
