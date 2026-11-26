"""The marketplace's plugins folders: dashboard plugins default to ``<data_dir>/dashboard-plugins`` (``data_dir`` from
the option, else QCLI_DATA_DIR), a file system failure while installing, updating or removing a dashboard or server
plugin answers 500 naming the folder and what to set (the commands print it), and a startup WARNING names a folder
that cannot be created or written."""

from __future__ import annotations

import errno
import logging
import os
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder, DashboardMarketplaceOptions
from qodalis_cli_admin.services.plugins_folder import (
    plugins_folder_error,
    unwritable_folder_code,
    unwritable_folders_warning,
)

from .command_helpers import V1, run_command
from .dashboard_marketplace_helpers import BASE, MARKET, TICKETS, FakeNet
from .test_server_plugin_marketplace import Host as ServerHost

DATA_HINT = "Set data_dir (or QCLI_DATA_DIR) to a writable folder"
root = hasattr(os, "geteuid") and os.geteuid() == 0
skip_as_root = pytest.mark.skipif(root, reason="root writes everywhere")


@pytest.fixture
def lock():
    """Makes a folder read-only until the test ends."""
    locked: list[str] = []

    def make(path: str) -> None:
        os.chmod(path, 0o555)
        locked.append(path)

    yield make
    for path in locked:
        os.chmod(path, 0o755)


class DataDirHost:
    """A server whose admin plugin keeps dashboard plugins in its data directory (no ``dir``)."""

    def __init__(self, net: FakeNet, data_dir: Optional[str] = None, dir: Optional[str] = None) -> None:
        # Notifications in memory: the security warning would create the data directory these tests lock
        self.result = create_cli_server(CliServerOptions(
            data_dir=data_dir, allow_anonymous_admin=True, notifications={"file": False},
        ))
        self.admin = CliAdminBuilder().set_marketplace(
            DashboardMarketplaceOptions(url=MARKET, fetch=net.fetch, dir=dir),
        ).set_credentials("admin", "pass").set_jwt_secret("folder-test-secret-0123456789abcdef").build(AdminBuildDeps(
            registry=self.result.registry, event_socket_manager=self.result.event_socket_manager, builder=self.result.builder,
        ))
        self.admin.log_buffer.uninstall_handler()
        self.result.mount_plugin(self.admin)
        self.client = TestClient(self.result.app)
        token = self.client.post(f"{V1}/auth/login", json={"username": "admin", "password": "pass"}).json()["token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def install(self, body: dict[str, Any]) -> Any:
        return self.client.post(f"{BASE}/installed", headers=self.headers, json=body)

    def update(self, name: str) -> Any:
        return self.client.put(f"{BASE}/installed/{name}", headers=self.headers, json={})

    def remove(self, name: str) -> Any:
        return self.client.delete(f"{BASE}/installed/{name}", headers=self.headers)

    def run(self, line: str) -> Any:
        return run_command(self.client, self.result, line)


def _tickets(net: FakeNet, versions: tuple[str, ...] = ("1.0.0",)) -> FakeNet:
    net.publish(TICKETS, [{"version": v} for v in versions], title="Tickets")
    return net


# --------------------------------------------------------------------------- helpers


def _fs_error(path: str, code: int = errno.EACCES) -> OSError:
    return OSError(code, f"secret text {os.strerror(code)}", path)


def test_names_the_folder_the_reason_and_the_hint_only_for_file_system_errors_on_it():
    folder = os.path.abspath("/srv/app/qcli-data/dashboard-plugins")
    message = f"cannot write the plugins folder {folder}: permission denied (EACCES). {DATA_HINT}"
    assert plugins_folder_error(_fs_error("/srv/app/qcli-data"), folder, DATA_HINT) == message
    assert plugins_folder_error(PermissionError(errno.EACCES, "x", os.path.join(folder, "installed.json.tmp")), folder, DATA_HINT) == message
    assert plugins_folder_error(_fs_error(folder, errno.EROFS), folder, DATA_HINT) == (
        f"cannot write the plugins folder {folder}: read-only file system (EROFS). {DATA_HINT}"
    )
    assert "no space left on device (ENOSPC)" in plugins_folder_error(_fs_error(folder, errno.ENOSPC), folder, DATA_HINT)
    # not about the folder: another path, no path, a plain error
    assert plugins_folder_error(_fs_error("/etc/other"), folder, DATA_HINT) is None
    assert plugins_folder_error(OSError(errno.ECONNREFUSED, "refused"), folder, DATA_HINT) is None
    assert plugins_folder_error(ValueError("boom"), folder, DATA_HINT) is None


@skip_as_root
def test_finds_a_folder_that_cannot_be_created_or_written_without_creating_it(tmp_path, lock):
    folder = str(tmp_path / "a" / "b")
    assert unwritable_folder_code(folder) is None
    assert not os.path.exists(tmp_path / "a")
    lock(str(tmp_path))
    assert unwritable_folder_code(folder) == "EACCES"
    assert unwritable_folders_warning([(str(tmp_path / "x"), DATA_HINT)]) == (
        f"the marketplace cannot write the plugins folder {tmp_path / 'x'}: permission denied (EACCES). "
        f"Installing, updating and removing plugins will fail. {DATA_HINT}."
    )
    assert unwritable_folders_warning([]) is None


# --------------------------------------------------------------------------- the data directory


def test_keeps_dashboard_plugins_in_the_data_directory(tmp_path):
    data_dir = str(tmp_path / "data")
    host = DataDirHost(_tickets(FakeNet()), data_dir)
    assert host.install({"name": TICKETS}).status_code == 201
    assert os.path.isfile(os.path.join(data_dir, "dashboard-plugins", "installed.json"))


def test_reads_qcli_data_dir_when_data_dir_is_not_given_and_data_dir_and_dir_win(tmp_path, monkeypatch):
    from_env = str(tmp_path / "env")
    monkeypatch.setenv("QCLI_DATA_DIR", from_env)
    net = _tickets(FakeNet())
    host = DataDirHost(net)
    assert host.result.builder.server_plugins.data_dir == os.path.abspath(from_env)
    assert host.result.builder.server_plugins.dir == os.path.join(os.path.abspath(from_env), "server-plugins")
    assert host.install({"name": TICKETS}).status_code == 201
    assert os.path.isfile(os.path.join(from_env, "dashboard-plugins", "installed.json"))

    own = str(tmp_path / "own")
    assert create_cli_server(CliServerOptions(data_dir=own)).builder.server_plugins.data_dir == os.path.abspath(own)
    explicit = str(tmp_path / "mine")
    other = DataDirHost(net, dir=explicit)
    assert other.install({"name": TICKETS}).status_code == 201
    assert os.path.isfile(os.path.join(explicit, "installed.json"))


# --------------------------------------------------------------------------- clear errors


@skip_as_root
def test_dashboard_plugins_install_update_and_remove_answer_500_naming_the_folder(tmp_path, lock):
    parent = tmp_path / "app"
    parent.mkdir()
    data_dir = str(parent / "qcli-data")
    host = DataDirHost(_tickets(FakeNet(), ("1.0.0", "1.1.0")), data_dir)
    folder = os.path.join(data_dir, "dashboard-plugins")
    expected = {"error": f"cannot write the plugins folder {folder}: permission denied (EACCES). {DATA_HINT}"}

    lock(str(parent))
    install = host.install({"name": TICKETS, "version": "1.0.0"})
    assert (install.status_code, install.json()) == (500, expected)
    assert host.run(f"admin dashboard install {TICKETS}@1.0.0").text == f"Error: {expected['error']}"

    # installed while writable, then the folder turns read-only
    os.chmod(parent, 0o755)
    assert host.install({"name": TICKETS, "version": "1.0.0"}).status_code == 201
    lock(folder)
    update = host.update(TICKETS)
    assert (update.status_code, update.json()) == (500, expected)
    remove = host.remove(TICKETS)
    assert (remove.status_code, remove.json()) == (500, expected)


@skip_as_root
def test_an_explicit_dir_names_the_marketplace_dir(tmp_path, lock):
    folder = str(tmp_path / "plugins")
    host = DataDirHost(_tickets(FakeNet()), str(tmp_path / "data"), dir=folder)
    lock(str(tmp_path))
    res = host.install({"name": TICKETS})
    assert (res.status_code, res.json()) == (500, {
        "error": f"cannot write the plugins folder {folder}: permission denied (EACCES). Set the marketplace's dir to a writable folder",
    })


@skip_as_root
def test_server_plugins_install_answers_500_naming_the_folder_and_the_command_prints_it(tmp_path, lock):
    host = ServerHost(str(tmp_path / "app" / "data"), anonymous_admin=True, notifications={"file": False})
    host.net.publish()
    lock(str(tmp_path))
    expected = f"cannot write the plugins folder {host.dir}: permission denied (EACCES). {DATA_HINT}"
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (500, {"error": expected}), res.text
    assert host.run("admin plugins install hello").text == f"Error: {expected}"
    host.close()


@skip_as_root
def test_logs_one_warning_at_startup_when_a_folder_cannot_be_written(tmp_path, lock, caplog):
    parent = tmp_path / "app"
    parent.mkdir()
    data_dir = str(parent / "qcli-data")
    with caplog.at_level(logging.WARNING, logger="qcli-admin"):
        DataDirHost(FakeNet(), data_dir)
    assert [r for r in caplog.records if "plugins folder" in r.getMessage()] == []

    lock(str(parent))
    with caplog.at_level(logging.WARNING, logger="qcli-admin"):
        DataDirHost(FakeNet(), data_dir)
    lines = [r.getMessage() for r in caplog.records if "plugins folder" in r.getMessage()]
    assert lines == [
        f"the marketplace cannot write the plugins folders {os.path.join(data_dir, 'dashboard-plugins')}: permission denied (EACCES); "
        f"{os.path.join(data_dir, 'server-plugins')}: permission denied (EACCES). "
        f"Installing, updating and removing plugins will fail. {DATA_HINT}.",
    ]
