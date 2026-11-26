"""Server plugins from the marketplace: the admin plugin's ``/api/v1/qcli/server/marketplace`` and ``admin plugins``
on a fake marketplace, PyPI and pip (no network): browsing, installs checked before anything is stored, the restart,
the next start loading the plugin, updates, removal, the trust policy and permissions."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from typing import Any, Optional

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder, DashboardMarketplaceOptions, read_wheel, satisfies_server_range

from .command_helpers import V1, add_users, protect, run_command
from .server_plugin_helpers import (
    FILES,
    HELLO_MODULE,
    HELLO_PACKAGE,
    MARKET,
    PYPI,
    SERVER_VERSION,
    FakePip,
    FakeServerNet,
    forget_fixture_modules,
    make_wheel,
    read_installed,
    sha256_hex,
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


def _seed_platforms(host: Any) -> None:
    """hello (Node and Python), nodeonly, dotnetonly (no Python package) and jobs (Python only)."""
    host.net.platform_filter = True
    host.net.publish(versions=["1.1.0", "1.2.0"], platforms=["node", "python", "dotnet"])
    host.net.publish("nodeonly", platforms=["node"])
    host.net.publish("dotnetonly", platforms=["dotnet"])
    host.net.publish("jobs", package="qodalis-cli-server-plugin-jobs", platforms=["python"])


def _list(host: Any, platform: Optional[str] = None) -> tuple[Any, dict[str, list[str]], dict[str, Any]]:
    res = host.client.get(f"{BASE}/plugins", params={} if platform is None else {"platform": platform}, headers=host.auth())
    query = parse_qs(urlsplit(host.net.calls[-1]).query, keep_blank_values=True)
    return res, query, {i["name"]: i for i in (res.json().get("items") or [])} if res.status_code == 200 else {}


_NOT_FOR_PYTHON = {"platform": None, "allowed": False, "blockedReason": "not available for python"}


def _has(item: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(item.get(k) == v for k, v in expected.items())


def test_platform_this_the_default_lists_this_platform_with_this_server_version(start):
    host = start()
    _seed_platforms(host)
    for platform in (None, "this", ""):
        res, query, items = _list(host, platform)
        assert res.status_code == 200
        assert query == {"kind": ["server"], "platform": ["python"], "serverVersion": [SERVER_VERSION]}
        assert sorted(items) == ["hello", "jobs"] and res.json()["total"] == 2
        assert items["hello"]["platform"] == {"package": HELLO_PACKAGE, "latest": "1.2.0", "installed": None, "compatible": True}


def test_platform_all_has_no_filter_and_lists_other_platforms_plugins_not_installable(start):
    host = start()
    _seed_platforms(host)
    res, query, items = _list(host, "all")
    assert res.status_code == 200
    assert query == {"kind": ["server"]}
    assert sorted(items) == ["dotnetonly", "hello", "jobs", "nodeonly"] and res.json()["total"] == 4
    assert _has(items["nodeonly"], {**_NOT_FOR_PYTHON, "otherPlatforms": ["node"], "installed": None, "updateAvailable": False})
    assert _has(items["dotnetonly"], _NOT_FOR_PYTHON)
    assert items["hello"]["platform"] == {"package": HELLO_PACKAGE, "latest": "1.2.0", "installed": None, "compatible": True}
    assert items["hello"]["otherPlatforms"] == ["node", "dotnet"] and items["hello"]["allowed"] is True


def test_platform_node_python_dotnet_are_passed_on_and_the_annotation_stays_this_servers(start):
    host = start()
    _seed_platforms(host)
    res, query, items = _list(host, "python")
    assert query == {"kind": ["server"], "platform": ["python"], "serverVersion": [SERVER_VERSION]}
    assert sorted(items) == ["hello", "jobs"]
    res, query, items = _list(host, "node")
    assert res.status_code == 200
    assert query == {"kind": ["server"], "platform": ["node"]}
    assert sorted(items) == ["hello", "nodeonly"] and res.json()["total"] == 2
    assert _has(items["nodeonly"], _NOT_FOR_PYTHON)
    assert items["hello"]["platform"]["package"] == HELLO_PACKAGE and items["hello"]["allowed"] is True
    res, query, items = _list(host, "dotnet")
    assert query == {"kind": ["server"], "platform": ["dotnet"]}
    assert sorted(items) == ["dotnetonly", "hello"]
    assert _has(items["dotnetonly"], _NOT_FOR_PYTHON)


def test_an_older_marketplace_ignoring_the_filter_other_platforms_plugins_are_not_installable(start):
    host = start()
    _seed_platforms(host)
    host.net.platform_filter = False
    _, _, items = _list(host, "this")
    assert sorted(items) == ["dotnetonly", "hello", "jobs", "nodeonly"]
    assert _has(items["nodeonly"], _NOT_FOR_PYTHON)
    _, _, items = _list(host, "node")
    # Node's view (not available for node) is not this server's: jobs keeps its Python package
    assert (items["jobs"]["compatible"], items["jobs"]["reason"]) == (False, "not available for node")
    assert items["jobs"]["platform"]["package"] == "qodalis-cli-server-plugin-jobs" and items["jobs"]["platform"]["compatible"] is True
    assert items["jobs"].get("blockedReason") != "not available for node"
    assert _has(items["dotnetonly"], _NOT_FOR_PYTHON)


def test_any_other_platform_is_400_asked_of_no_marketplace(start):
    host = start()
    _seed_platforms(host)
    for platform in ("java", "NODE", "this,all"):
        res = host.client.get(f"{BASE}/plugins", params={"platform": platform}, headers=host.auth())
        assert (res.status_code, res.json()) == (400, {"error": f"invalid platform: {platform} (this, all, node, python, dotnet)"})
    twice = host.client.get(f"{BASE}/plugins?platform=all&platform=node", headers=host.auth())
    assert (twice.status_code, twice.json()) == (400, {"error": "invalid platform: all,node (this, all, node, python, dotnet)"})
    assert host.net.calls == []


def test_admin_plugins_search_platform(start):
    host = start(anonymous_admin=True)
    _seed_platforms(host)
    out = host.run("admin plugins search --platform all").text
    assert any(line.startswith("nodeonly") and "(not for python)" in line and line.rstrip().endswith("blocked: not available for python") for line in out.splitlines())
    assert "platform" not in parse_qs(urlsplit(host.net.calls[-1]).query)
    out = host.run("admin plugins search --platform dotnet").text
    assert "dotnetonly" in out and "nodeonly" not in out
    assert parse_qs(urlsplit(host.net.calls[-1]).query)["platform"] == ["dotnet"]
    assert "nodeonly" not in host.run("admin plugins search --platform this").text
    assert host.run("admin plugins search --platform java").text == "Error: invalid platform: java (this, all, node, python, dotnet)"


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
    host.net.add_pypi("qcli-test-missing-dep", "0.9")
    sha = host.net.add_pypi("qcli-test-missing-dep", "1.3", requires=["fastapi>=0.1", "colorama; sys_platform == 'never'"])
    host.net.add_pypi("qcli-test-missing-dep", "2.0", wheel=False)  # newest, no wheel: skipped
    # pip never resolves: whatever it might bring is dropped anyway, it never shadows the server's
    host.pip.brings = ["fastapi"]
    assert host.install({"name": "hello"}).status_code == 202
    deps = host.pip.calls[1]
    assert deps[:2] == ["install", "--target"] and "--no-deps" in deps and "--require-hashes" in deps
    assert deps[deps.index("--only-binary") + 1] == ":all:"
    assert host.pip.requirements == f"qcli-test-missing-dep==1.3 --hash=sha256:{sha}\n"
    infos = sorted(d for d in os.listdir(host.dir) if d.endswith(".dist-info"))
    assert infos == ["qcli_hello_plugin-1.2.0.dist-info", "qcli_test_missing_dep-9.9.9.dist-info"]


def test_without_pins_every_transitive_dependency_is_resolved_trusted_and_hash_pinned(start):
    host = start(trusted=["qcli-test-*"])
    host.net.publish(wheel=lambda v: make_wheel(v, requires=["qcli-test-a>=1"]))
    a = host.net.add_pypi("qcli-test-a", "1.0", requires=["qcli-test-b[fast]<3", "fastapi"])
    b = host.net.add_pypi("qcli-test-b", "2.5", requires=["qcli-test-c; extra == 'fast'", "qcli-test-d; extra == 'slow'"])
    c = host.net.add_pypi("qcli-test-c", "0.1")
    host.net.add_pypi("qcli-test-b", "3.0")
    assert host.install({"name": "hello"}).status_code == 202, host.pip.calls
    assert host.pip.requirements == (
        f"qcli-test-a==1.0 --hash=sha256:{a}\n"
        f"qcli-test-b==2.5 --hash=sha256:{b}\n"
        f"qcli-test-c==0.1 --hash=sha256:{c}\n"
    )
    assert "--no-deps" in host.pip.calls[1]


def test_without_pins_an_untrusted_transitive_dependency_is_refused(start):
    host = start(trusted=["qcli-test-a"])
    host.net.publish(wheel=lambda v: make_wheel(v, requires=["qcli-test-a"]))
    host.net.add_pypi("qcli-test-a", "1.0", requires=["evil-package>=1"])
    host.net.add_pypi("evil-package", "1.0")
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (400, {"error": (
        "untrusted dependency: evil-package (needed by qcli-test-a; the marketplace pins none and the server does not "
        "trust it)"
    )})
    assert host.pip.calls == [] and not os.path.exists(os.path.join(host.dir, "installed.json"))


@pytest.mark.parametrize("wheel_requires,pypi_requires", [
    (["qcli-test-a @ https://evil.test/qcli_test_a-1.0-py3-none-any.whl"], None),
    (["qcli-test-a"], ["qcli-test-b @ git+https://evil.test/b.git"]),
])
def test_a_direct_url_requirement_is_refused_even_trusted(start, wheel_requires, pypi_requires):
    host = start(trusted=["*"])
    host.net.publish(wheel=lambda v: make_wheel(v, requires=wheel_requires))
    host.net.add_pypi("qcli-test-a", "1.0", requires=pypi_requires)
    res = host.install({"name": "hello"})
    assert res.status_code == 400
    assert res.json()["error"].startswith("direct-URL dependency refused: qcli-test-")
    assert host.pip.calls == [] and not os.path.exists(os.path.join(host.dir, "installed.json"))


def test_a_direct_url_requirement_is_refused_with_pins_too(start):
    host = start()
    digest = "sha256-" + base64.b64encode(hashlib.sha256(b"dep").digest()).decode()
    host.net.publish(wheel=lambda v: make_wheel(v, requires=["qcli-test-a @ https://evil.test/a.whl"]), dependencies=[
        {"id": "qcli-test-a", "version": "1.0", "integrity": digest, "range": "*"},
    ])
    res = host.install({"name": "hello"})
    assert res.json() == {"error": f"direct-URL dependency refused: qcli-test-a (needed by {HELLO_PACKAGE})"}
    assert host.pip.calls == []


def test_without_pins_conflicting_requirements_are_refused(start):
    host = start(trusted=["qcli-test-*"])
    host.net.publish(wheel=lambda v: make_wheel(v, requires=["qcli-test-a", "qcli-test-b"]))
    host.net.add_pypi("qcli-test-a", "1.0", requires=["qcli-test-b<2"])
    host.net.add_pypi("qcli-test-b", "2.0")
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (400, {"error": "conflicting dependency: qcli-test-a needs qcli-test-b<2, 2.0 is chosen"})
    assert host.pip.calls == []


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


def _no_wheel_installed(host: Any) -> None:
    assert host.pip.calls == [] and host.pending() == []
    assert not os.path.exists(os.path.join(host.dir, "installed.json"))


def test_the_wheel_is_the_one_the_marketplace_lists_with_its_hash(start):
    host = start()
    host.net.publish(versions=["1.2.0"])
    # PyPI also has a wheel the marketplace does not list, first and as fitting: never chosen
    release = host.net.releases[(HELLO_PACKAGE, "1.2.0")]
    other = make_wheel("1.2.0", requires=[])
    release["urls"].insert(0, {
        "filename": f"{HELLO_MODULE}-1.2.0-py2.py3-none-any.whl", "url": f"{FILES}/unlisted.whl",
        "packagetype": "bdist_wheel", "digests": {"sha256": sha256_hex(other)},
    })
    host.net.files[f"{FILES}/unlisted.whl"] = other
    res = host.install({"name": "hello"})
    assert res.status_code == 202, res.text
    assert f"{FILES}/unlisted.whl" not in host.net.calls
    assert host.pip.calls[0][-1].endswith(f"{HELLO_MODULE}-1.2.0-py3-none-any.whl")


def test_a_wheel_the_marketplace_does_not_list_is_refused_never_pypis_hash_alone(start):
    host = start()
    host.net.publish(versions=["1.2.0"], marketplace_files=[
        {"path": f"{HELLO_MODULE}-1.2.0.tar.gz", "integrity": "sha256-" + "B" * 43 + "="},
    ])
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (
        502, {"error": f"the marketplace lists no wheel of {HELLO_PACKAGE}==1.2.0 for this Python "
                       f"({sys.version_info.major}.{sys.version_info.minor})"},
    )
    _no_wheel_installed(host)


def test_the_marketplace_and_pypi_disagreeing_on_the_listed_file_is_refused(start):
    host = start()
    host.net.publish(versions=["1.2.0"], marketplace_files=[
        {"path": f"{HELLO_MODULE}-1.2.0-py3-none-any.whl", "integrity": "sha256-" + "C" * 43 + "="},
    ])
    res = host.install({"name": "hello"})
    assert (res.status_code, res.json()) == (502, {"error": "integrity mismatch: the marketplace and PyPI disagree"})
    _no_wheel_installed(host)


def test_without_files_the_marketplace_integrity_must_name_a_pypi_wheel(start):
    host = start()
    host.net.publish(versions=["1.2.0"], marketplace_files=None)
    assert host.install({"name": "hello"}).status_code == 202  # an older marketplace: its integrity of the wheel

    bare = start("bare")
    bare.net.publish(versions=["1.2.0"], marketplace_files=None, marketplace_integrity=None)
    res = bare.install({"name": "hello"})
    assert (res.status_code, res.json()) == (502, {"error": f"the marketplace has no sha256 of {HELLO_PACKAGE}==1.2.0"})
    _no_wheel_installed(bare)


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
    assert host.install({"name": "hello"}).json() == {"error": f"already added in code: {HELLO_PACKAGE}"}


def test_a_plugin_added_in_code_is_installed_with_source_code_and_install_is_refused(start):
    from qodalis_cli.models.cli_installed_plugin import CliInstalledPlugin

    host = start()
    host.result.builder.add_installed_plugin(CliInstalledPlugin(id=HELLO_PACKAGE, name="Hello", version="1.1.0", source="code"))
    host.net.publish(versions=["1.1.0", "1.2.0"])
    host.net.publish("other", package="qcli-other-plugin")
    items = {i["name"]: i for i in host.get("/plugins").json()["items"]}
    assert items["hello"]["installed"] == {"version": "1.1.0", "source": "code"}
    assert items["hello"]["updateAvailable"] is True and items["hello"]["platform"]["installed"] is None
    assert items["other"]["installed"] is None
    assert host.get("/plugins/hello").json()["installed"] == {"version": "1.1.0", "source": "code"}
    res = host.install({"name": "hello"})
    assert res.status_code == 409
    assert res.json() == {"error": f"already added in code: {HELLO_PACKAGE}@1.1.0"}
    assert not os.path.isfile(os.path.join(host.dir, "installed.json"))


# --------------------------------------------------------------------------- update and remove


def test_update_replaces_the_files_and_remove_deletes_them_clearing_the_restart(start):
    host = start()
    host.net.publish(versions=["1.1.0", "1.2.0"])
    assert host.install({"name": "hello", "version": "1.1.0"}).status_code == 202
    # installed from the marketplace: source marketplace, updatable here
    listed = next(i for i in host.get("/plugins").json()["items"] if i["name"] == "hello")
    assert listed["installed"] == {"version": "1.1.0", "source": "marketplace"} and listed["updateAvailable"] is True
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


def test_remove_deletes_the_bytecode_python_wrote_for_its_modules(start):
    host = start()
    host.net.publish()
    assert host.install({"name": "hello"}).status_code == 202
    package = os.path.join(host.dir, HELLO_MODULE)
    modules = [n[:-3] for n in os.listdir(package) if n.endswith(".py")]
    cache = os.path.join(package, "__pycache__")
    os.makedirs(cache)
    for m in modules:  # as an import writes them (RECORD never lists them)
        for tag in ("cpython-314", "cpython-314.opt-1"):
            with open(os.path.join(cache, f"{m}.{tag}.pyc"), "wb") as f:
                f.write(b"pyc")
    # A directory shared with another package keeps that package's bytecode
    shared_cache = os.path.join(host.dir, "__pycache__")
    os.makedirs(shared_cache)
    with open(os.path.join(shared_cache, "other_module.cpython-314.pyc"), "wb") as f:
        f.write(b"pyc")
    assert host.remove("hello").status_code == 202
    assert not os.path.exists(package)
    assert os.listdir(shared_cache) == ["other_module.cpython-314.pyc"]


def test_a_plugin_waiting_for_its_settings_is_listed_with_its_version_and_settings_provider(start, monkeypatch):
    first = start()
    first.net.publish()
    assert first.install({"name": "hello"}).status_code == 202
    assert "settings" not in first.get("/installed").json()[0]  # not loaded yet: no provider
    forget_fixture_modules()
    monkeypatch.setenv("QCLI_HELLO_NEEDS_CONFIGURATION", "1")
    host = start(first.data_dir.rsplit(os.sep, 1)[1], net=first.net, lifespan=True)
    [plugin] = host.get("/installed").json()
    assert (plugin["status"], plugin["loadedVersion"], plugin["missing"], plugin["settings"]) == (
        "needs-configuration", "1.2.0", ["settings.apiKey"], "hello",
    )
    assert "error" not in plugin
    # Removed, it still runs (waiting) until a restart
    assert host.remove("hello").json()["status"] == "removed"
    assert [(p["name"], p["status"], p["loadedVersion"]) for p in host.get("/installed").json()] == [
        ("hello", "removed", "1.2.0"),
    ]


def test_removing_a_loaded_plugin_keeps_it_running_until_a_restart(start, tmp_path):
    first = start()
    first.net.publish()
    assert first.install({"name": "hello"}).status_code == 202
    forget_fixture_modules()
    host = start(first.data_dir.rsplit(os.sep, 1)[1], net=first.net, lifespan=True)
    installed = host.get("/installed").json()
    assert [(p["name"], p["status"], p["loadedVersion"]) for p in installed] == [("hello", "loaded", "1.2.0")]
    assert installed[0]["settings"] == "hello"
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


def test_every_marketplace_lookup_names_the_server_kind(start):
    host = start()
    host.net.publish(versions=["1.1.0", "1.2.0"])
    assert host.install({"name": "hello", "version": "1.1.0"}).status_code == 202
    assert host.update("hello", {}).status_code == 202
    assert host.get("/plugins?query=hello").status_code == 200
    lookups = [u for u in host.net.calls if u.startswith(MARKET)]
    assert any("/plugins/hello" in u for u in lookups)
    assert all(parse_qs(urlsplit(u).query).get("kind") == ["server"] for u in lookups), lookups


REFUSED_ANONYMOUS = "Error: sign-in required: protect the server (users plugin) or set allow_anonymous_admin=True (plugins:install)"


def test_admin_plugins_install_update_remove_refused_to_anonymous_callers_reads_allowed(start):
    host = start()
    host.net.publish(versions=["1.1.0", "1.2.0"])
    for line in ("admin plugins install hello@1.1.0", "admin plugins update hello", "admin plugins remove hello"):
        res = host.run(line)
        assert (res.text, res.exit_code) == (REFUSED_ANONYMOUS, 1)  # exit code 1, as on Node
    assert host.pip.calls == []
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
    not_found = host.run("admin plugins install nope")
    assert (not_found.text, not_found.exit_code) == ("Error: plugin not found: nope", 0)  # only refusals exit with 1
