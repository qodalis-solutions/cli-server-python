"""Server plugins from the marketplace: the admin plugin's ``/api/v1/qcli/server/marketplace`` and ``admin plugins``
on a fake marketplace, PyPI and pip (no network): browsing, installs checked before anything is stored, the restart,
the next start loading the plugin, updates, removal, the trust policy and permissions."""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder, DashboardMarketplaceOptions, read_wheel, satisfies_server_range

from .command_helpers import V1, add_users, protect, run_command
from .server_plugin_helpers import (
    HELLO_PACKAGE,
    MARKET,
    PYPI,
    SERVER_VERSION,
    FakePip,
    FakeServerNet,
    forget_fixture_modules,
    make_wheel,
    read_installed,
)

BASE = f"{V1}/server/marketplace"


@pytest.fixture(autouse=True)
def _clean_modules():
    forget_fixture_modules()
    yield
    forget_fixture_modules()


class Host:
    """A server with the admin plugin (password sign-in, or the users plugin), the fake marketplace and pip."""

    def __init__(
        self, data_dir: str, *, net: Optional[FakeServerNet] = None, install: Optional[str] = None,
        users: Optional[dict[str, list[str]]] = None, server_plugins: Any = None, lifespan: bool = False,
        trusted: Optional[list[str]] = None, anonymous_admin: bool = False,
    ) -> None:
        self.net = net or FakeServerNet()
        self.pip = FakePip()
        self.data_dir = data_dir
        self.result = create_cli_server(CliServerOptions(
            data_dir=data_dir, server_plugins=server_plugins, allow_anonymous_admin=anonymous_admin,
        ))
        self.audit: list[Any] = []
        self.events: list[Any] = []
        if users is None:
            audit = self.audit

            class _Store:
                async def log(self, entry: Any) -> None:
                    audit.append(entry)

            self.result.builder.set_config_audit_store(_Store())
        self.result.event_bus.subscribe("server:*", self.events.append)
        self.users = protect(self.result) if users is not None else None
        builder = CliAdminBuilder().set_marketplace(DashboardMarketplaceOptions(
            url=MARKET, dir=os.path.join(data_dir, "dashboard-plugins"), fetch=self.net.fetch, install=install,
            pypi_url=PYPI, pip=self.pip, server_version=SERVER_VERSION, trusted_dependencies=trusted,
        ))
        secret = "server-marketplace-test-secret-0123456789"
        if self.users is not None:
            builder.set_users_plugin(self.users).set_jwt_secret(secret)
        else:
            builder.set_credentials("admin", "pass").set_jwt_secret(secret)
        self.admin = builder.build(AdminBuildDeps(
            registry=self.result.registry, event_socket_manager=self.result.event_socket_manager, builder=self.result.builder,
        ))
        self.admin.log_buffer.uninstall_handler()
        self.result.mount_plugin(self.admin)
        self.client = TestClient(self.result.app)
        if lifespan:
            self.client.__enter__()
        self._lifespan = lifespan
        if users is not None:
            self.tokens = add_users(self.client, users)
            self.token = self.tokens["root"]
        else:
            self.tokens = {}
            self.token = self.client.post(f"{V1}/auth/login", json={"username": "admin", "password": "pass"}).json()["token"]

    @property
    def dir(self) -> str:
        return self.result.server_plugins.dir

    def auth(self, user: Optional[str] = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[user] if user else self.token}"}

    def get(self, path: str, user: Optional[str] = None) -> Any:
        return self.client.get(f"{BASE}{path}", headers=self.auth(user))

    def install(self, body: dict[str, Any], user: Optional[str] = None) -> Any:
        return self.client.post(f"{BASE}/installed", headers=self.auth(user), json=body)

    def update(self, name: str, body: dict[str, Any], user: Optional[str] = None) -> Any:
        return self.client.put(f"{BASE}/installed/{name}", headers=self.auth(user), json=body)

    def remove(self, name: str, user: Optional[str] = None) -> Any:
        return self.client.delete(f"{BASE}/installed/{name}", headers=self.auth(user))

    def run(self, line: str, user: Optional[str] = None) -> Any:
        return run_command(self.client, self.result, line, self.tokens[user] if user else None)

    def pending(self) -> list[str]:
        return [p["id"] for p in self.result.restart.status()["pending"]]

    def close(self) -> None:
        if self._lifespan:
            self.client.__exit__(None, None, None)
        self.admin.log_buffer.uninstall_handler()


@pytest.fixture
def start(tmp_path: Any):
    hosts: list[Host] = []

    def make(data: str = "data", **kwargs: Any) -> Host:
        host = Host(str(tmp_path / data), **kwargs)
        hosts.append(host)
        return host

    yield make
    for h in hosts:
        h.close()


# --------------------------------------------------------------------------- helpers


def test_server_ranges_and_wheels():
    assert satisfies_server_range("1.0.0b31", ">=1.0.0b7")
    assert not satisfies_server_range("1.0.0b29", ">=1.0.0b30")
    assert satisfies_server_range("1.0.0b31", None) and satisfies_server_range("1.0.0b31", "*")
    info = read_wheel(make_wheel("2.0.0", requires=["qodalis-cli-server>=1.0.0b7", "rich>=13"]))
    assert (info.name, info.version, info.entry_points) == (HELLO_PACKAGE, "2.0.0", ["hello"])
    assert info.requires == ["qodalis-cli-server>=1.0.0b7", "rich>=13"]


# --------------------------------------------------------------------------- browsing


def test_info_search_and_details_are_annotated_for_python(start):
    host = start()
    host.net.publish()
    host.net.publish("nodeonly", platforms=["node"])
    host.net.publish("needs", needs_configuration=["settings.url"])
    assert host.get("").json() == {
        "marketplaceUrl": MARKET, "install": "verified", "platform": "python", "serverVersion": SERVER_VERSION, "enabled": True,
    }
    items = {i["name"]: i for i in host.get("/plugins").json()["items"]}
    assert items["hello"]["platform"] == {"package": HELLO_PACKAGE, "latest": "1.2.0", "installed": None, "compatible": True}
    assert items["hello"]["otherPlatforms"] == ["node"] and items["hello"]["allowed"] is True
    assert items["nodeonly"]["blockedReason"] == "not available for python"
    details = host.get("/plugins/needs").json()
    assert details["needsConfiguration"] == ["settings.url"]
    python = next(v for v in details["versions"] if v["platform"] == "python")
    assert python["allowed"] is True
    assert "kind=server" in host.net.calls[0] and "platform=python" in host.net.calls[0] and f"serverVersion={SERVER_VERSION}" in host.net.calls[0]
    assert host.get("/plugins/nope").status_code == 404


def test_an_unreachable_marketplace_is_502(start):
    host = start()
    host.net.marketplace_down = True
    res = host.get("/plugins")
    assert (res.status_code, res.json()) == (502, {"error": "marketplace unreachable"})


# --------------------------------------------------------------------------- install


def test_install_verifies_the_wheel_installs_it_marks_a_restart_and_the_next_start_loads_it(start, tmp_path):
    host = start()
    host.net.publish(needs_configuration=["settings.url"])
    res = host.install({"name": "hello"})
    assert res.status_code == 202, res.text
    body = res.json()
    assert (body["name"], body["package"], body["version"], body["status"], body["restartRequired"]) == (
        "hello", HELLO_PACKAGE, "1.2.0", "pending", True,
    )
    assert body["needsConfiguration"] == ["settings.url"]
    record = read_installed(host.dir)[0]
    assert record["integrity"].startswith("sha256-") and record["file"].endswith(".whl") and record["installedBy"] == "admin"
    assert os.path.isfile(os.path.join(host.dir, "qcli_hello_plugin", "plugin.py"))
    assert os.path.isdir(os.path.join(host.dir, "qcli_hello_plugin-1.2.0.dist-info"))
    assert not [d for d in os.listdir(host.dir) if d.startswith(".staging")]
    assert host.pip.calls[0][:4] == ["install", "--target", host.pip.calls[0][2], "--no-deps"]
    assert host.pending() == ["plugin:hello"]
    assert [e.event for e in host.audit] == ["server.plugin.install"]
    assert host.audit[0].properties == {"name": "hello", "package": HELLO_PACKAGE, "version": "1.2.0"}
    assert [e.type for e in host.events if e.type == "server:plugins-changed"] == ["server:plugins-changed"]
    assert host.install({"name": "hello"}).status_code == 409
    # The next start loads it
    forget_fixture_modules()
    again = create_cli_server(CliServerOptions(data_dir=host.data_dir))
    with TestClient(again.app) as client:
        assert {s.name: s.status for s in again.server_plugins.states()} == {"hello": "loaded"}
        assert client.get(f"{V1}/hello").json()["greeting"] == "Hello"


DEPS = [
    "qodalis-cli-server>=1.0.0b7", "qodalis-cli-server-abstractions>=1.0.0b7", "fastapi>=0.100",
    "qcli-test-missing-dep>=1.0", "colorama; sys_platform == 'never'", "extra-only; extra == 'docs'",
]


def test_without_pins_a_dependency_the_host_does_not_trust_is_refused(start):
    host = start()
    host.net.publish(wheel=lambda v: make_wheel(v, requires=DEPS))
    res = host.install({"name": "hello"})
    assert res.status_code == 400
    assert res.json()["error"] == (
        "untrusted dependency: qcli-test-missing-dep (the marketplace pins none and the server does not trust it)"
    )
    assert host.pip.calls == [] and not os.path.exists(os.path.join(host.dir, "installed.json"))


@pytest.mark.parametrize("trusted", [["qcli-test-missing-dep"], ["qcli-test-*"], ["QCLI_Test_Missing_Dep"]])
def test_without_pins_trusted_dependencies_install_never_the_servers_packages(start, trusted):
    host = start(trusted=trusted)
    host.net.publish(wheel=lambda v: make_wheel(v, requires=DEPS))
    # pip resolving the tree brings a package the server has: dropped, it never shadows the server's
    host.pip.brings = ["fastapi", "qcli_test_transitive"]
    assert host.install({"name": "hello"}).status_code == 202
    deps = host.pip.calls[1]
    assert deps[:2] == ["install", "--target"] and "--no-deps" not in deps and "--require-hashes" not in deps
    assert [a for a in deps if not a.startswith("-") and a != ":all:"][2:] == ["qcli-test-missing-dep>=1.0"]
    infos = sorted(d for d in os.listdir(host.dir) if d.endswith(".dist-info"))
    assert infos == ["qcli_hello_plugin-1.2.0.dist-info", "qcli_test_missing_dep-9.9.9.dist-info", "qcli_test_transitive-9.9.9.dist-info"]


def test_pinned_dependencies_install_hash_checked_without_resolving(start):
    host = start()
    digest = "sha256-" + base64.b64encode(hashlib.sha256(b"dep").digest()).decode()
    host.net.publish(wheel=lambda v: make_wheel(v, requires=DEPS), dependencies=[
        {"id": "qcli-test-missing-dep", "version": "1.4.0", "integrity": digest, "range": ">=1.0"},
        {"id": "qcli-test-transitive", "version": "2.0.0", "integrity": digest, "range": ">=2"},
        {"id": "fastapi", "version": "0.1.0", "integrity": digest, "range": "*"},
    ])
    assert host.install({"name": "hello"}).status_code == 202, host.pip.calls
    deps = host.pip.calls[1]
    assert "--require-hashes" in deps and "--no-deps" in deps and "-r" in deps
    hex_digest = hashlib.sha256(b"dep").hexdigest()
    assert host.pip.requirements == (
        f"qcli-test-missing-dep==1.4.0 --hash=sha256:{hex_digest}\n"
        f"qcli-test-transitive==2.0.0 --hash=sha256:{hex_digest}\n"
    )
    assert not [f for f in os.listdir(host.dir) if f.startswith(".requirements")]


def test_a_dependency_the_marketplace_did_not_pin_is_refused(start):
    host = start(trusted=["*"])
    digest = "sha256-" + base64.b64encode(hashlib.sha256(b"dep").digest()).decode()
    host.net.publish(wheel=lambda v: make_wheel(v, requires=DEPS), dependencies=[
        {"id": "qcli-test-other", "version": "1.0.0", "integrity": digest, "range": "*"},
    ])
    res = host.install({"name": "hello"})
    assert res.json() == {"error": "untrusted dependency: qcli-test-missing-dep (not pinned by the marketplace)"}
    assert host.pip.calls == []


def test_a_package_of_the_server_at_another_version_is_never_installed_beside_it(start):
    host = start(trusted=["*"])
    host.net.publish(wheel=lambda v: make_wheel(v, requires=["fastapi>=999"]))
    res = host.install({"name": "hello"})
    assert res.status_code == 400 and res.json()["error"].startswith("incompatible dependency: needs fastapi>=999, the server has ")
    assert host.pip.calls == []


@pytest.mark.parametrize("change,status,message", [
    ({"marketplace_integrity": "sha256-" + "A" * 43 + "="}, 502, "integrity mismatch: the marketplace and PyPI disagree"),
    ({"wheel": lambda v: make_wheel(v, entry_points="")}, 400, f"not a server plugin package: {HELLO_PACKAGE} (no qodalis_cli.plugins entry point)"),
    ({"wheel": lambda v: make_wheel(v, requires=["qodalis-cli-server>=2.0"])}, 400,
     f"incompatible: {HELLO_PACKAGE}==1.2.0 needs qodalis-cli-server>=2.0 (this server has {SERVER_VERSION})"),
    ({"server_range": ">=1.0.0b40"}, 400, f"incompatible: {HELLO_PACKAGE}==1.2.0 needs server >=1.0.0b40 (this server has {SERVER_VERSION})"),
    ({"verified": False}, 403, "publisher not verified"),
    ({"platforms": ["node"]}, 400, "not available for python: hello"),
    ({"auto_install": False}, 400, "needs setup in code: needs the host's code"),
    ({"wheel": lambda v: make_wheel(v, name="other-package")}, 502, f"the wheel is other-package==1.2.0, not {HELLO_PACKAGE}==1.2.0"),
])
def test_a_failed_check_stores_nothing(start, change, status, message):
    host = start()
    host.net.publish(**change)
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (status, {"error": message})
    assert host.pip.calls == [] and host.pending() == []
    assert not os.path.exists(os.path.join(host.dir, "installed.json"))


def test_a_wheel_that_does_not_match_pypis_hash_is_refused(start):
    host = start()
    host.net.publish()
    url = next(iter(host.net.files))
    host.net.files[url] = make_wheel("1.2.0", requires=[])
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (502, {"error": f"integrity mismatch: {HELLO_PACKAGE}==1.2.0"})
    assert host.pip.calls == []


def test_pip_failing_is_502_and_leaves_nothing(start):
    host = start()
    host.net.publish()
    host.pip.fail = "No space left on device"
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (502, {"error": "pip install failed (exit 1): ERROR: No space left on device"})
    assert not os.path.exists(os.path.join(host.dir, "installed.json"))
    assert not os.path.isdir(os.path.join(host.dir, "qcli_hello_plugin"))
    assert os.listdir(os.path.join(host.dir, "wheels")) == []


def test_a_version_and_the_install_policy(start):
    host = start(install="off")
    host.net.publish(versions=["1.1.0", "1.2.0"])
    assert host.install({"name": "hello"}).json() == {"error": "install disabled"}
    listed = start("listed", install="listed")
    listed.net.publish(versions=["1.1.0", "1.2.0"], verified=False)
    res = listed.install({"name": "hello", "version": "1.1.0"})
    assert res.status_code == 202 and res.json()["version"] == "1.1.0"
    assert listed.install({"name": "nope"}).status_code == 404
    assert listed.install({"name": "hello", "version": "bad version"}).json() == {"error": "invalid version"}


def test_server_plugins_disabled_refuses_changes(start):
    host = start(server_plugins=False)
    host.net.publish()
    assert host.get("").json()["enabled"] is False
    assert host.install({"name": "hello"}).json() == {"error": "server plugins disabled"}


def test_a_package_the_servers_environment_has_is_enabled_without_a_download(start):
    host = start()
    host.net.publish("weather", package="qodalis-cli-server-plugin-weather")
    res = host.install({"name": "weather"})
    assert res.status_code == 202, res.text
    record = read_installed(host.dir)[0]
    assert record["bundled"] is True and record["integrity"] == ""
    assert host.pip.calls == [] and not [c for c in host.net.calls if c.startswith(PYPI)]
    assert host.update("weather", {}).json()["error"].endswith("it is updated with the server, not from here")
    assert host.remove("weather").status_code == 202
    import qodalis_cli_weather

    assert os.path.isfile(qodalis_cli_weather.__file__)


def test_a_package_the_host_set_up_is_refused(start):
    host = start()
    host.result.builder.add_installed_plugin("qcli-hello-plugin")
    host.net.publish()
    assert host.install({"name": "hello"}).json() == {"error": f"already installed by the host: {HELLO_PACKAGE}"}


# --------------------------------------------------------------------------- update and remove


def test_update_replaces_the_files_and_remove_deletes_them_clearing_the_restart(start):
    host = start()
    host.net.publish(versions=["1.1.0", "1.2.0"])
    assert host.install({"name": "hello", "version": "1.1.0"}).status_code == 202
    res = host.update("hello", {})
    assert res.status_code == 202
    assert (res.json()["version"], res.json()["previousVersion"], res.json()["changed"]) == ("1.2.0", "1.1.0", True)
    assert sorted(d for d in os.listdir(host.dir) if d.endswith(".dist-info")) == ["qcli_hello_plugin-1.2.0.dist-info"]
    assert os.listdir(os.path.join(host.dir, "wheels")) == ["qcli_hello_plugin-1.2.0-py3-none-any.whl"]
    same = host.update("hello", {})
    assert same.status_code == 200 and same.json()["changed"] is False
    assert host.pending() == ["plugin:hello"]
    removed = host.remove("hello")
    assert removed.status_code == 202 and removed.json()["restartRequired"] is False
    assert host.pending() == []
    assert not os.path.isdir(os.path.join(host.dir, "qcli_hello_plugin"))
    assert not [d for d in os.listdir(host.dir) if d.endswith(".dist-info")]
    assert read_installed(host.dir) == []
    assert [e.event for e in host.audit] == ["server.plugin.install", "server.plugin.update", "server.plugin.remove"]
    assert host.remove("hello").status_code == 404


def test_removing_a_loaded_plugin_keeps_it_running_until_a_restart(start, tmp_path):
    first = start()
    first.net.publish()
    assert first.install({"name": "hello"}).status_code == 202
    forget_fixture_modules()
    host = start(first.data_dir.rsplit(os.sep, 1)[1], net=first.net, lifespan=True)
    installed = host.get("/installed").json()
    assert [(p["name"], p["status"], p["loadedVersion"]) for p in installed] == [("hello", "loaded", "1.2.0")]
    removed = host.remove("hello").json()
    assert (removed["status"], removed["restartRequired"]) == ("removed", True)
    assert [(p["name"], p["status"]) for p in host.get("/installed").json()] == [("hello", "removed")]
    assert host.pending() == ["plugin:hello"]


# --------------------------------------------------------------------------- permissions and the terminal


def test_changes_need_plugins_install(start):
    host = start(users={"viewer": ["admin:access"], "installer": ["admin:access", "plugins:install"]})
    host.net.publish()
    assert host.get("/plugins", "viewer").status_code == 200
    res = host.install({"name": "hello"}, "viewer")
    # The users plugin refuses it by the route's permission
    assert (res.status_code, res.json()) == (403, {"error": "missing permission: plugins:install"})
    assert host.install({"name": "hello"}, "installer").status_code == 202
    assert host.remove("hello", "viewer").status_code == 403


REFUSED_ANONYMOUS = "Error: sign-in required: protect the server (users plugin) or set allow_anonymous_admin=True (plugins:install)"


def test_admin_plugins_install_update_remove_refused_to_anonymous_callers_reads_allowed(start):
    host = start()
    host.net.publish(versions=["1.1.0", "1.2.0"])
    assert host.run("admin plugins install hello@1.1.0").text == REFUSED_ANONYMOUS
    assert host.pip.calls == []
    assert host.run("admin plugins update hello").text == REFUSED_ANONYMOUS
    assert host.run("admin plugins remove hello").text == REFUSED_ANONYMOUS
    assert "installable" in host.run("admin plugins search hello").text
    assert host.run("admin plugins installed").text.startswith("No server plugins installed")


def test_admin_plugins_search_installed_install_update_remove(start):
    host = start(anonymous_admin=True)
    host.net.publish(versions=["1.1.0", "1.2.0"])
    search = host.run("admin plugins search hello").text
    assert "hello" in search and HELLO_PACKAGE in search and "installable" in search
    out = host.run("admin plugins install hello@1.1.0").text
    assert out == f"Installed hello: {HELLO_PACKAGE}==1.1.0.\nRestart the server to apply it: admin restart (or the dashboard)."
    listed = host.run("admin plugins installed").text
    assert "pending restart" in listed and "1.1.0" in listed
    assert host.run("admin plugins update hello").text.startswith("Updated hello from 1.1.0 to 1.2.0.")
    assert host.run("admin plugins update hello").text == "hello is already at 1.2.0."
    assert host.run("admin plugins remove hello").text.startswith(f"Removed hello ({HELLO_PACKAGE}==1.2.0).")
    assert host.run("admin plugins install nope").text == "Error: plugin not found: nope"
