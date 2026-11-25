"""Dashboard plugins from the marketplace (admin plugin): browsing with annotations, the trust policy (verified, listed,
off), the dashboard API, downloads from jsDelivr then unpkg checked against the marketplace's sha384 (a mismatch
stores nothing), an unreachable marketplace, install / update / remove, live registration and serving at
/qcli/modules/<id>/, persistence across a restart, id clashes, path-traversal-safe names, permissions on a protected
server, audit and events, and the ``admin dashboard`` command.  A fake marketplace and CDN: no network."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import os
import re
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from qodalis_cli import create_cli_server
from qodalis_cli_admin import (
    AdminBuildDeps,
    CliAdminBuilder,
    DashboardMarketplaceOptions,
    MarketplaceResponse,
    compare_dashboard_plugin_versions,
    dashboard_plugin_dir_name,
    dashboard_plugin_id,
    is_valid_dashboard_plugin_name,
    python_server_package,
    sha384_integrity,
)

from .command_helpers import ROOT_PASS, V1, add_users, protect, run_command

MARKET = "https://market.test"
API = f"{MARKET}/api/v1/qcli/marketplace"
JSDELIVR = "https://cdn.jsdelivr.net/npm"
UNPKG = "https://unpkg.com"
BASE = f"{V1}/dashboard/marketplace"
TICKETS = "@acme/cli-dashboard-tickets"
JOBS = "@qodalis/cli-dashboard-module-jobs"
_UNSET: Any = object()


def integrity_of(text: str) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(text.encode()).digest()).decode()


def response(status: int, body: Any) -> MarketplaceResponse:
    data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return MarketplaceResponse(status, data, {"content-length": str(len(data))})


class FakeNet:
    """The marketplace's public API and the two CDNs, in memory."""

    def __init__(self) -> None:
        self.plugins: dict[str, dict[str, Any]] = {}
        self.files: dict[str, str] = {}
        self.calls: list[str] = []
        self.marketplace_down = False
        #: Answer details without ``files`` (an older marketplace): the version record has them.
        self.files_only_in_version_record = False
        #: Ignore the ``kind`` filter (the marketplace before dashboard plugins): every plugin listed and counted.
        self.ignores_kind = False

    async def fetch(self, url: str, headers: Any, timeout: float) -> MarketplaceResponse:
        self.calls.append(url)
        if url.startswith(MARKET):
            if self.marketplace_down:
                raise ConnectionError("fetch failed")
            return self._marketplace(url)
        body = self.files.get(url)
        return response(404, {"error": "not found"}) if body is None else response(200, body)

    def publish(
        self, name: str, versions: list[dict[str, Any]], *, verified: bool = True, kind: str = "dashboard",
        title: Optional[str] = None, icon: Optional[str] = None, server_packages: Any = None,
        extension_id: Optional[str] = None,
    ) -> None:
        records = []
        for v in versions:
            js = v.get("js") or f"export default {{ id: '{name}', version: '{v['version']}' }};"
            css = v.get("css", _UNSET)
            if css is _UNSET:
                css = f".x-{re.sub(r'[^A-Za-z0-9_]', '', v['version'])} {{ color: red; }}"
            files = [{"path": "dist/extension.js", "integrity": integrity_of(js), "size": len(js.encode())}]
            if css is not None:
                files.append({"path": "dist/extension.css", "integrity": integrity_of(css), "size": len(css.encode())})

            def put(cdn: str, path: str, published: str, served: Any) -> None:
                if served is None:
                    return
                self.files[f"{cdn}/{name}@{v['version']}/{path}"] = published if served is _UNSET else served

            put(JSDELIVR, "dist/extension.js", js, v.get("jsdelivr", _UNSET))
            put(UNPKG, "dist/extension.js", js, v.get("unpkg", _UNSET))
            if css is not None:
                put(JSDELIVR, "dist/extension.css", css, None if v.get("jsdelivr", _UNSET) is None else _UNSET)
                put(UNPKG, "dist/extension.css", css, _UNSET)
            record = {
                "version": v["version"], "status": v.get("status", "listed"), "kind": kind, "files": files,
                "dashboardApi": v.get("dashboardApi", 1), "publishedAt": "2026-11-20T10:00:00.000Z",
            }
            if server_packages:
                record["serverPackages"] = server_packages
            records.append(record)
        listed = sorted(
            (r["version"] for r in records if r["status"] == "listed"),
            key=functools.cmp_to_key(compare_dashboard_plugin_versions),
        )
        latest = listed[-1] if listed else None
        scope = name[: name.index("/")] if name.startswith("@") else name
        plugin: dict[str, Any] = {
            "name": name, "shortName": name.split("/")[-1], "description": f"The {name} page", "kind": kind,
        }
        if title:
            plugin["title"] = title
        if icon:
            plugin["icon"] = icon
        plugin["publisher"] = {"id": scope.lstrip("@"), "name": scope, "verified": verified}
        plugin["latest"] = latest
        plugin["version"] = latest
        plugin["dashboardApi"] = next((r["dashboardApi"] for r in records if r["version"] == latest), 1)
        if server_packages:
            plugin["serverPackages"] = server_packages
        if extension_id:
            plugin["extension"] = {"id": extension_id}
        plugin["versions"] = records
        self.plugins[name] = plugin

    def _marketplace(self, url: str) -> MarketplaceResponse:
        parts = urlsplit(url)
        path = parts.path[len("/api/v1/qcli/marketplace"):]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if path == "/plugins":
            text = query.get("query", "").lower()
            kind = query.get("kind")
            items = [
                {k: v for k, v in p.items() if k not in ("versions", "extension")}
                for p in self.plugins.values()
                if (not kind or self.ignores_kind or p["kind"] == kind) and text in p["name"]
            ]
            return response(200, {
                "items": items, "total": len(items), "page": int(query.get("page", 1)), "pageSize": int(query.get("pageSize", 24)),
            })
        m = re.fullmatch(r"/plugins/(.+?)(?:/versions/([^/]+))?", path)
        if m:
            plugin = self.plugins.get(unquote(m.group(1)))
            if not plugin:
                return response(404, {"error": "plugin not found"})
            if m.group(2):
                v = next((x for x in plugin["versions"] if x["version"] == unquote(m.group(2))), None)
                return response(200, v) if v else response(404, {"error": "version not found"})
            if self.files_only_in_version_record:
                return response(200, {
                    **plugin, "versions": [{k: x for k, x in v.items() if k != "files"} for v in plugin["versions"]],
                })
            return response(200, plugin)
        return response(404, {"error": "not found"})


class Host:
    """A server with the admin plugin (password sign-in, or the users plugin) and the fake marketplace."""

    def __init__(
        self, tmp_path: Any, *, net: Optional[FakeNet] = None, dir: Optional[str] = None, install: Optional[str] = None,
        configure: Any = None, users: Optional[dict[str, list[str]]] = None,
    ) -> None:
        self.net = net or FakeNet()
        self.dir = dir or str(tmp_path / f"plugins-{id(self)}")
        os.makedirs(self.dir, exist_ok=True)
        self.result = create_cli_server()
        self.audit: list[Any] = []
        self.events: list[Any] = []
        if users is None:
            audit = self.audit

            class _Store:
                async def log(self, entry: Any) -> None:
                    audit.append(entry)

            self.result.builder.set_config_audit_store(_Store())
        self.result.event_bus.subscribe("dashboard:*", self.events.append)
        if configure:
            configure(self.result)
        self.users = protect(self.result) if users is not None else None
        builder = CliAdminBuilder().set_marketplace(
            DashboardMarketplaceOptions(url=MARKET, dir=self.dir, fetch=self.net.fetch, install=install),
        )
        if self.users is not None:
            builder.set_users_plugin(self.users).set_jwt_secret("marketplace-test-secret-0123456789abcdef")
        else:
            builder.set_credentials("admin", "pass").set_jwt_secret("marketplace-test-secret-0123456789abcdef")
        self.admin = builder.build(AdminBuildDeps(
            registry=self.result.registry, event_socket_manager=self.result.event_socket_manager, builder=self.result.builder,
        ))
        self.admin.log_buffer.uninstall_handler()
        self.result.mount_plugin(self.admin)
        self.client = TestClient(self.result.app)
        if users is not None:
            self.tokens = add_users(self.client, users)
            self.token = self.tokens["root"]
        else:
            self.tokens = {}
            res = self.client.post(f"{V1}/auth/login", json={"username": "admin", "password": "pass"})
            self.token = res.json()["token"]

    def auth(self, user: Optional[str] = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[user] if user else self.token}"}

    def get(self, path: str, user: Optional[str] = None) -> Any:
        return self.client.get(path, headers=self.auth(user))

    def post(self, body: dict[str, Any], user: Optional[str] = None) -> Any:
        return self.client.post(f"{BASE}/installed", headers=self.auth(user), json=body)

    def put(self, name: str, body: dict[str, Any], user: Optional[str] = None) -> Any:
        return self.client.put(f"{BASE}/installed/{name}", headers=self.auth(user), json=body)

    def delete(self, name: str, user: Optional[str] = None) -> Any:
        return self.client.delete(f"{BASE}/installed/{name}", headers=self.auth(user))

    def extensions(self) -> list[dict[str, Any]]:
        return self.get(f"{V1}/dashboard/extensions").json()["extensions"]

    def run(self, line: str, user: Optional[str] = None) -> Any:
        return run_command(self.client, self.result, line, self.tokens[user] if user else None)

    def close(self) -> None:
        self.admin.log_buffer.uninstall_handler()


@pytest.fixture
def start(tmp_path: Any):
    hosts: list[Host] = []

    def make(**kwargs: Any) -> Host:
        host = Host(tmp_path, **kwargs)
        hosts.append(host)
        return host

    yield make
    for h in hosts:
        h.close()


def non_market(calls: list[str]) -> list[str]:
    return [c for c in calls if not c.startswith(MARKET)]


# --------------------------------------------------------------------------- helpers


def test_derives_the_page_id_official_modules_by_suffix_else_the_extension_id_else_the_name():
    assert dashboard_plugin_id("@qodalis/cli-dashboard-module-jobs") == "jobs"
    assert dashboard_plugin_id("@qodalis/cli-dashboard-module-data-explorer", "other") == "data-explorer"
    assert dashboard_plugin_id("@acme/cli-dashboard-tickets", "helpdesk") == "helpdesk"
    assert dashboard_plugin_id("@acme/cli-dashboard-tickets", "Bad Id") == "tickets"
    assert dashboard_plugin_id("@acme/cli-dashboard-plugin-crm") == "crm"
    assert dashboard_plugin_id("my.page_v2") == "my-page-v2"
    assert dashboard_plugin_id("___") is None


def test_keeps_names_inside_the_storage_directory():
    assert dashboard_plugin_dir_name("@acme/tickets") == "@acme+tickets"
    assert dashboard_plugin_dir_name("tickets") == "tickets"
    for bad in ["../etc", "..", ".hidden", "@acme/../../x", "a/b", "@acme/x/y", "UPPER", "a\\b", "", "x" * 215, "@/x", "a\0b", "x\n"]:
        assert is_valid_dashboard_plugin_name(bad) is False
        with pytest.raises(Exception, match="invalid package name"):
            dashboard_plugin_dir_name(bad)


def test_compares_versions_as_semver_prereleases_before_their_release():
    versions = ["1.0.0", "1.0.0-beta.10", "1.0.0-beta.2", "0.9.9", "1.0.0-alpha", "1.10.0", "1.2.0"]
    assert sorted(versions, key=functools.cmp_to_key(compare_dashboard_plugin_versions)) == [
        "0.9.9", "1.0.0-alpha", "1.0.0-beta.2", "1.0.0-beta.10", "1.0.0", "1.2.0", "1.10.0",
    ]


def test_reads_the_python_server_package_from_either_form_and_sha384_as_sri():
    assert python_server_package({"node": "@qodalis/cli-server-plugin-jobs", "python": "qodalis-cli-server-plugin-jobs"}) == "qodalis-cli-server-plugin-jobs"
    assert python_server_package([{"ecosystem": "npm", "name": "x"}, {"ecosystem": "pypi", "name": "y", "install": "pip install y"}]) == "y"
    assert python_server_package(None) is None
    assert python_server_package({"node": "x"}) is None
    assert sha384_integrity(b"abc") == integrity_of("abc")


# --------------------------------------------------------------------------- browsing


def test_get_root_reports_the_marketplace_the_policy_and_the_dashboard_api(start):
    h = start()
    res = h.get(BASE)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    assert res.json() == {"marketplaceUrl": MARKET, "install": "verified", "dashboardApi": 1}
    assert h.client.get(BASE).status_code == 401


def test_get_plugins_proxies_kind_dashboard_and_annotates_each_item(start):
    net = FakeNet()
    net.publish(JOBS, [{"version": "1.0.0"}, {"version": "1.1.0"}], icon="⏱️", title="Jobs",
                server_packages={"python": "qodalis-cli-server-plugin-jobs"})
    net.publish(TICKETS, [{"version": "2.0.0"}], verified=False)
    net.publish("@acme/cli-dashboard-next", [{"version": "3.0.0", "dashboardApi": 2}])
    net.publish("@acme/web-thing", [{"version": "1.0.0"}], kind="web")
    h = start(net=net)
    res = h.get(f"{BASE}/plugins?query=&page=1&pageSize=10")
    assert res.status_code == 200
    list_url = next(c for c in net.calls if c.startswith(f"{API}/plugins?"))
    params = parse_qs(urlsplit(list_url).query)
    assert (params["kind"], params["dashboardApi"], params["pageSize"]) == (["dashboard"], ["1"], ["10"])
    body = res.json()
    assert (body["total"], body["page"], body["pageSize"]) == (3, 1, 10)
    by_name = {i["name"]: i for i in body["items"]}
    assert sorted(by_name) == sorted(["@acme/cli-dashboard-next", TICKETS, JOBS])
    jobs = by_name[JOBS]
    assert (jobs["kind"], jobs["publisher"]["verified"], jobs["installed"], jobs["updateAvailable"], jobs["allowed"]) == (
        "dashboard", True, None, False, True,
    )
    assert jobs["requires"] == {"serverPackage": "qodalis-cli-server-plugin-jobs", "installed": False}
    assert "blockedReason" not in jobs
    assert (by_name[TICKETS]["allowed"], by_name[TICKETS]["blockedReason"], by_name[TICKETS]["requires"]) == (
        False, "unverified publisher", None,
    )
    assert (by_name["@acme/cli-dashboard-next"]["allowed"], by_name["@acme/cli-dashboard-next"]["blockedReason"]) == (
        False, "needs dashboard API 2",
    )


def test_a_marketplace_that_ignores_the_kind_filter_only_its_dashboard_plugins_counted_on_their_own(start):
    net = FakeNet()
    net.ignores_kind = True
    net.publish(JOBS, [{"version": "1.0.0"}])
    for name in ["@acme/web-one", "@acme/web-two", "@acme/web-three"]:
        net.publish(name, [{"version": "1.0.0"}], kind="web")
    h = start(net=net)
    res = h.get(f"{BASE}/plugins")
    assert res.status_code == 200
    assert [i["name"] for i in res.json()["items"]] == [JOBS]
    assert res.json()["total"] == 1
    # Only web plugins: an empty list with nothing counted (the live marketplace today)
    del net.plugins[JOBS]
    none = h.get(f"{BASE}/plugins").json()
    assert (none["items"], none["total"]) == ([], 0)


def test_requires_installed_follows_the_server_plugins_the_server_has(start):
    net = FakeNet()
    net.publish(JOBS, [{"version": "1.0.0"}], server_packages=[
        {"ecosystem": "npm", "name": "@qodalis/cli-server-plugin-jobs"},
        {"ecosystem": "pypi", "name": "qodalis-cli-server-plugin-admin", "install": "pip install qodalis-cli-server-plugin-admin"},
    ])
    h = start(net=net)
    # The admin plugin's own package is installed (mounted)
    assert h.get(f"{BASE}/plugins").json()["items"][0]["requires"] == {
        "serverPackage": "qodalis-cli-server-plugin-admin", "installed": True,
    }


def test_install_listed_allows_unverified_publishers_and_install_off_blocks_everything(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "2.0.0"}], verified=False)
    listed = start(net=net, install="listed")
    assert listed.get(f"{BASE}/plugins").json()["items"][0]["allowed"] is True
    off = start(net=net, install="off")
    assert off.get(BASE).json()["install"] == "off"
    item = off.get(f"{BASE}/plugins").json()["items"][0]
    assert (item["allowed"], item["blockedReason"]) == (False, "install disabled")


def test_get_plugin_returns_details_and_versions_annotated_scoped_names_raw_or_encoded(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.1.0", "status": "flagged"}, {"version": "2.0.0", "dashboardApi": 2}])
    h = start(net=net)
    for path in [f"{BASE}/plugins/{TICKETS}", f"{BASE}/plugins/{quote(TICKETS, safe='')}"]:
        res = h.get(path)
        assert res.status_code == 200
        body = res.json()
        assert (body["name"], body["installed"], body["allowed"], body["updateAvailable"]) == (TICKETS, None, True, False)
        versions = {v["version"]: v for v in body["versions"]}
        assert versions["1.0.0"]["allowed"] is True
        assert [f["path"] for f in versions["1.0.0"]["files"]] == ["dist/extension.js", "dist/extension.css"]
        assert (versions["1.1.0"]["allowed"], versions["1.1.0"]["blockedReason"]) == (False, "version flagged")
        assert (versions["2.0.0"]["allowed"], versions["2.0.0"]["blockedReason"]) == (False, "needs dashboard API 2")
    assert h.get(f"{BASE}/plugins/@acme/missing").json() == {"error": "plugin not found: @acme/missing"}
    assert h.get(f"{BASE}/plugins/..%2F..%2Fetc").status_code == 400


def test_an_unreachable_marketplace_is_a_502(start):
    net = FakeNet()
    net.marketplace_down = True
    h = start(net=net)
    for res in [h.get(f"{BASE}/plugins"), h.get(f"{BASE}/plugins/{TICKETS}"), h.post({"name": TICKETS})]:
        assert res.status_code == 502
        assert res.json() == {"error": "marketplace unreachable"}
    assert os.listdir(h.dir) == []
    # a marketplace answering 500 counts as unreachable too
    broken = FakeNet()

    async def fail(url: str, headers: Any, timeout: float) -> MarketplaceResponse:
        return response(500, {"error": "boom"})

    broken.fetch = fail  # type: ignore[method-assign]
    b = start(net=broken)
    assert b.get(f"{BASE}/plugins").status_code == 502


def test_rejects_bad_paging_parameters(start):
    h = start()
    assert h.get(f"{BASE}/plugins?page=0").json() == {"error": "invalid page"}
    assert h.get(f"{BASE}/plugins?pageSize=101").json() == {"error": "invalid pageSize"}


# --------------------------------------------------------------------------- install, update, remove


def test_installs_the_newest_fitting_version_stored_registered_live_and_served(start):
    net = FakeNet()
    net.publish(JOBS, [{"version": "1.0.0"}, {"version": "1.1.0"}, {"version": "2.0.0", "dashboardApi": 2}, {"version": "1.2.0", "status": "pending"}],
                title="Jobs", icon="⏱️")
    h = start(net=net)
    assert not any(e["id"] == "jobs" for e in h.extensions())

    res = h.post({"name": JOBS})
    assert res.status_code == 201
    body = res.json()
    assert body == {"name": JOBS, "version": "1.1.0", "title": "Jobs", "icon": "⏱️", "id": "jobs",
                    "installedAt": body["installedAt"], "installedBy": "admin"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", body["installedAt"])

    # stored: <dir>/<safe-name>/<version>/{extension.js,extension.css} and installed.json, no staging leftovers
    version_dir = os.path.join(h.dir, "@qodalis+cli-dashboard-module-jobs", "1.1.0")
    assert sorted(os.listdir(version_dir)) == ["extension.css", "extension.js"]
    assert sorted(os.listdir(h.dir)) == ["@qodalis+cli-dashboard-module-jobs", "installed.json"]
    with open(os.path.join(h.dir, "installed.json"), encoding="utf-8") as f:
        saved = json.load(f)
    record = saved["plugins"][0]
    assert len(saved["plugins"]) == 1
    assert {k: record[k] for k in ("name", "version", "id", "title", "icon", "installedAt", "installedBy")} == {
        "name": JOBS, "version": "1.1.0", "id": "jobs", "title": "Jobs", "icon": "⏱️",
        "installedAt": body["installedAt"], "installedBy": "admin",
    }
    assert [(f["file"], f["path"]) for f in record["files"]] == [("extension.js", "dist/extension.js"), ("extension.css", "dist/extension.css")]
    assert all(f["integrity"].startswith("sha384-") and isinstance(f["size"], int) for f in record["files"])
    # downloaded from jsDelivr (unpkg not needed)
    assert non_market(net.calls) == [f"{JSDELIVR}/{JOBS}@1.1.0/dist/extension.js", f"{JSDELIVR}/{JOBS}@1.1.0/dist/extension.css"]

    # live in the extensions list, marked as from the marketplace
    page = next(e for e in h.extensions() if e["id"] == "jobs")
    assert {k: page[k] for k in ("id", "title", "icon", "version", "source")} == {
        "id": "jobs", "title": "Jobs", "icon": "⏱️", "version": "1.1.0", "source": "marketplace",
    }
    assert re.fullmatch(r"/qcli/modules/jobs/extension\.js\?v=[0-9a-f]{12}", page["entry"])
    assert len(page["styles"]) == 1 and re.fullmatch(r"/qcli/modules/jobs/extension\.css\?v=[0-9a-f]{12}", page["styles"][0])

    # served from this server's origin, no sign-in, with an ETag
    js = h.client.get(page["entry"])
    assert js.status_code == 200
    assert re.match(r"^(text|application)/javascript", js.headers["content-type"])
    assert js.headers["cache-control"] == "no-cache"
    assert "version: '1.1.0'" in js.text
    assert h.client.get("/qcli/modules/jobs/extension.js", headers={"If-None-Match": js.headers["etag"]}).status_code == 304
    assert h.client.get(page["styles"][0]).headers["content-type"].startswith("text/css")
    assert h.client.get("/qcli/modules/jobs/installed.json").status_code == 404
    assert h.client.get("/qcli/modules/jobs/..%2F..%2Finstalled.json").status_code == 404
    assert h.client.get("/qcli/modules/other/extension.js").status_code == 404

    # audit and event
    assert len(h.audit) == 1
    entry = h.audit[0]
    assert (entry.event, entry.username, entry.actor, entry.target, entry.details, entry.properties) == (
        "dashboard.plugin.install", "admin", "admin", JOBS, f"{JOBS}@1.1.0", {"name": JOBS, "version": "1.1.0", "id": "jobs"},
    )
    assert [(e.type, e.data, e.audience.permission) for e in h.events] == [
        ("dashboard:extensions-changed", {"action": "install", "name": JOBS, "version": "1.1.0", "id": "jobs"}, "admin:access"),
    ]

    # GET /installed, the list item now annotated
    assert h.get(f"{BASE}/installed").json() == [{**body, "updateAvailable": False}]
    assert h.get(f"{BASE}/plugins").json()["items"][0]["installed"] == {"version": "1.1.0"}


def test_installs_a_given_version_and_refuses_unknown_unlisted_invalid_and_too_new_versions(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.1.0", "status": "flagged"}, {"version": "2.0.0", "dashboardApi": 2}])
    h = start(net=net)
    assert h.post({"name": TICKETS, "version": "9.9.9"}).json() == {"error": f"version not found: {TICKETS}@9.9.9"}
    assert h.post({"name": TICKETS, "version": "1.1.0"}).status_code == 404
    assert h.post({"name": TICKETS, "version": "2.0.0"}).json() == {"error": "needs dashboard API 2"}
    assert h.post({"name": TICKETS, "version": "../1"}).json() == {"error": "invalid version"}
    assert h.post({}).json() == {"error": "invalid package name"}
    assert h.post({"name": "@acme/missing"}).json() == {"error": "plugin not found: @acme/missing"}
    ok = h.post({"name": TICKETS, "version": "1.0.0"})
    assert ok.status_code == 201
    assert (ok.json()["version"], ok.json()["id"], ok.json()["title"], ok.json()["icon"]) == ("1.0.0", "tickets", "cli-dashboard-tickets", None)
    assert h.post({"name": TICKETS}).json() == {"error": f"already installed: {TICKETS}@1.0.0"}


def test_refuses_a_newer_dashboard_api_a_web_plugin_and_bad_file_records(start):
    net = FakeNet()
    net.publish("@acme/cli-dashboard-next", [{"version": "3.0.0", "dashboardApi": 2}])
    net.publish("@acme/web-thing", [{"version": "1.0.0"}], kind="web")
    net.publish("@acme/cli-dashboard-bad", [{"version": "1.0.0"}])
    net.plugins["@acme/cli-dashboard-bad"]["versions"][0]["files"][0]["path"] = "../../evil.js"
    h = start(net=net)
    assert h.post({"name": "@acme/cli-dashboard-next"}).json() == {"error": "needs dashboard API 2"}
    assert h.post({"name": "@acme/web-thing"}).json() == {"error": "not a dashboard plugin: @acme/web-thing"}
    bad = h.post({"name": "@acme/cli-dashboard-bad"})
    assert bad.status_code == 502
    assert bad.json() == {"error": "invalid marketplace record: files"}
    assert os.listdir(h.dir) == []


def test_trust_policy_verified_refuses_unverified_listed_allows_off_refuses_all(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}], verified=False)
    verified = start(net=net)
    refused = verified.post({"name": TICKETS})
    assert refused.status_code == 403
    assert refused.json() == {"error": "publisher not verified"}
    assert os.listdir(verified.dir) == []
    assert non_market(net.calls) == []

    listed = start(net=net, install="listed")
    assert listed.post({"name": TICKETS}).status_code == 201

    off = start(net=net, install="off")
    disabled = off.post({"name": TICKETS})
    assert disabled.status_code == 403
    assert disabled.json() == {"error": "install disabled"}


def test_an_integrity_mismatch_on_every_cdn_is_a_502_and_stores_nothing(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0", "jsdelivr": "tampered", "unpkg": "tampered too"}])
    h = start(net=net)
    res = h.post({"name": TICKETS})
    assert res.status_code == 502
    assert res.json() == {"error": "integrity mismatch: dist/extension.js"}
    assert os.listdir(h.dir) == []
    assert h.audit == [] and h.events == []
    assert not any(e["id"] == "tickets" for e in h.extensions())


def test_falls_back_to_unpkg_and_both_failing_is_download_failed(start):
    net = FakeNet()
    net.publish("@acme/cli-dashboard-a", [{"version": "1.0.0", "jsdelivr": None}])
    net.publish("@acme/cli-dashboard-b", [{"version": "1.0.0", "jsdelivr": "tampered"}])
    net.publish("@acme/cli-dashboard-c", [{"version": "1.0.0", "css": None, "jsdelivr": None, "unpkg": None}])
    h = start(net=net)
    assert h.post({"name": "@acme/cli-dashboard-a"}).status_code == 201
    assert f"{UNPKG}/@acme/cli-dashboard-a@1.0.0/dist/extension.js" in net.calls
    assert h.post({"name": "@acme/cli-dashboard-b"}).status_code == 201
    with open(os.path.join(h.dir, "@acme+cli-dashboard-b", "1.0.0", "extension.js"), encoding="utf-8") as f:
        assert f.read() != "tampered"
    failed = h.post({"name": "@acme/cli-dashboard-c"})
    assert failed.status_code == 502
    assert failed.json() == {"error": "download failed: dist/extension.js"}
    assert not os.path.exists(os.path.join(h.dir, "@acme+cli-dashboard-c"))


def test_refuses_files_over_5_mb(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0", "js": "x" * (5 * 1024 * 1024 + 1), "css": None}])
    h = start(net=net)
    res = h.post({"name": TICKETS})
    assert res.status_code == 502
    assert res.json() == {"error": "file too large: dist/extension.js"}


def test_reads_the_files_from_the_version_record_when_the_details_leave_them_out(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0", "css": None}])
    net.files_only_in_version_record = True
    h = start(net=net)
    assert h.post({"name": TICKETS}).status_code == 201
    assert f"{API}/plugins/{quote(TICKETS, safe='')}/versions/1.0.0" in net.calls
    assert os.listdir(os.path.join(h.dir, "@acme+cli-dashboard-tickets", "1.0.0")) == ["extension.js"]


def test_an_id_taken_by_another_extension_is_a_409(start):
    net = FakeNet()
    net.publish(JOBS, [{"version": "1.0.0"}])
    net.publish("@acme/cli-dashboard-billing", [{"version": "1.0.0"}], extension_id="billing")
    h = start(net=net, configure=lambda r: r.builder.add_dashboard_extension(
        {"id": "jobs", "title": "Jobs", "entry": "/qcli/modules/jobs/extension.js"},
    ))
    res = h.post({"name": JOBS})
    assert res.status_code == 409
    assert res.json() == {"error": "dashboard extension already registered: jobs"}
    assert os.listdir(h.dir) == []
    # the extension's own id; a second package with that id clashes with the first
    assert h.post({"name": "@acme/cli-dashboard-billing"}).json()["id"] == "billing"
    net.publish("@other/billing-page", [{"version": "1.0.0"}], extension_id="billing")
    assert h.post({"name": "@other/billing-page"}).status_code == 409


def test_path_traversal_in_names_is_refused_before_anything_is_fetched_or_written(start):
    h = start()
    for name in ["../../etc/passwd", "..", "@acme/../x", "a/b/c", "/abs", "C:\\x"]:
        res = h.post({"name": name})
        assert res.status_code == 400
        assert res.json() == {"error": "invalid package name"}
    assert h.put("..%2F..%2Fetc", {}).status_code == 400
    assert h.delete("..%2F..").status_code == 400
    assert h.net.calls == []
    assert os.listdir(h.dir) == []


def test_updates_to_the_newest_version_same_page_id_old_version_deleted(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}])
    h = start(net=net)
    assert h.post({"name": TICKETS}).status_code == 201
    old_entry = next(e for e in h.extensions() if e["id"] == "tickets")["entry"]

    # nothing newer: unchanged
    same = h.put(TICKETS, {})
    assert same.status_code == 200
    assert same.json()["version"] == "1.0.0"
    assert len(h.audit) == 1

    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.2.0"}])
    assert h.get(f"{BASE}/installed").json()[0]["updateAvailable"] is True
    item = h.get(f"{BASE}/plugins").json()["items"][0]
    assert (item["installed"], item["updateAvailable"]) == ({"version": "1.0.0"}, True)

    res = h.put(quote(TICKETS, safe=""), {})
    assert res.status_code == 200
    assert (res.json()["name"], res.json()["version"], res.json()["id"]) == (TICKETS, "1.2.0", "tickets")
    assert os.listdir(os.path.join(h.dir, "@acme+cli-dashboard-tickets")) == ["1.2.0"]
    pages = [e for e in h.extensions() if e["id"] == "tickets"]
    assert len(pages) == 1
    assert pages[0]["version"] == "1.2.0"
    assert pages[0]["entry"] != old_entry
    assert "version: '1.2.0'" in h.client.get("/qcli/modules/tickets/extension.js").text
    update = h.audit[1]
    assert (update.event, update.details, update.properties) == (
        "dashboard.plugin.update", f"{TICKETS} 1.0.0 -> 1.2.0",
        {"name": TICKETS, "version": "1.2.0", "id": "tickets", "previousVersion": "1.0.0"},
    )
    assert [e.data["action"] for e in h.events] == ["install", "update"]
    assert h.events[1].data == {"action": "update", "name": TICKETS, "version": "1.2.0", "id": "tickets", "previousVersion": "1.0.0"}
    with open(os.path.join(h.dir, "installed.json"), encoding="utf-8") as f:
        assert [p["version"] for p in json.load(f)["plugins"]] == ["1.2.0"]

    # back to a given version
    assert h.put(TICKETS, {"version": "1.0.0"}).json()["version"] == "1.0.0"
    # a failed update keeps the installed version
    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.3.0", "jsdelivr": "bad", "unpkg": "bad"}])
    assert h.put(TICKETS, {}).status_code == 502
    assert h.get(f"{BASE}/installed").json()[0]["version"] == "1.0.0"
    assert h.client.get("/qcli/modules/tickets/extension.js").status_code == 200
    assert h.put("@acme/other", {}).json() == {"error": "not installed: @acme/other"}


def test_removes_a_plugin_page_unregistered_files_deleted_audited_and_published(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}])
    net.publish(JOBS, [{"version": "1.0.0"}])
    h = start(net=net)
    assert h.post({"name": TICKETS}).status_code == 201
    assert h.post({"name": JOBS}).status_code == 201
    res = h.delete(TICKETS)
    assert res.status_code == 200
    assert (res.json()["name"], res.json()["version"], res.json()["id"]) == (TICKETS, "1.0.0", "tickets")
    assert not os.path.exists(os.path.join(h.dir, "@acme+cli-dashboard-tickets"))
    assert os.path.isfile(os.path.join(h.dir, "@qodalis+cli-dashboard-module-jobs", "1.0.0", "extension.js"))
    ids = [e["id"] for e in h.extensions()]
    assert "jobs" in ids and "tickets" not in ids
    assert h.client.get("/qcli/modules/tickets/extension.js").status_code == 404
    assert [a.event for a in h.audit] == ["dashboard.plugin.install", "dashboard.plugin.install", "dashboard.plugin.remove"]
    assert h.events[2].data == {"action": "remove", "name": TICKETS, "version": "1.0.0", "id": "tickets"}
    with open(os.path.join(h.dir, "installed.json"), encoding="utf-8") as f:
        assert [p["name"] for p in json.load(f)["plugins"]] == [JOBS]
    assert h.delete(TICKETS).json() == {"error": f"not installed: {TICKETS}"}


async def test_concurrent_installs_of_one_plugin_one_wins_the_other_is_a_409(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}])
    h = start(net=net)
    async with AsyncClient(transport=ASGITransport(app=h.result.app), base_url="http://test") as client:
        a, b = await asyncio.gather(
            client.post(f"{BASE}/installed", headers=h.auth(), json={"name": TICKETS}),
            client.post(f"{BASE}/installed", headers=h.auth(), json={"name": TICKETS}),
        )
    assert sorted([a.status_code, b.status_code]) == [201, 409]


# --------------------------------------------------------------------------- restart


def test_reloads_installed_plugins_on_start_offline_and_serves_them_again(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}], icon="🎫", title="Tickets")
    first = start(net=net)
    assert first.post({"name": TICKETS}).status_code == 201

    offline = FakeNet()
    offline.marketplace_down = True
    second = start(net=offline, dir=first.dir)
    page = next(e for e in second.extensions() if e["id"] == "tickets")
    assert {k: page[k] for k in ("id", "title", "icon", "version", "source")} == {
        "id": "tickets", "title": "Tickets", "icon": "🎫", "version": "1.0.0", "source": "marketplace",
    }
    assert second.client.get(page["entry"]).status_code == 200
    # installed still listed; updateAvailable left out while the marketplace is unreachable
    installed = second.get(f"{BASE}/installed").json()
    assert installed == [{"name": TICKETS, "version": "1.0.0", "title": "Tickets", "icon": "🎫", "id": "tickets",
                          "installedAt": installed[0]["installedAt"], "installedBy": "admin"}]
    # and removable without the marketplace
    assert second.delete(TICKETS).status_code == 200
    assert non_market(offline.calls) == []


def test_skips_tampered_files_and_taken_ids_and_survives_a_broken_installed_json(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}])
    net.publish(JOBS, [{"version": "1.0.0"}])
    first = start(net=net)
    assert first.post({"name": TICKETS}).status_code == 201
    assert first.post({"name": JOBS}).status_code == 201
    with open(os.path.join(first.dir, "@acme+cli-dashboard-tickets", "1.0.0", "extension.js"), "w", encoding="utf-8") as f:
        f.write('alert("tampered")')

    second = start(net=net, dir=first.dir, configure=lambda r: r.builder.add_dashboard_extension(
        {"id": "jobs", "title": "Host jobs", "entry": "/jobs.js"},
    ))
    extensions = second.extensions()
    assert [e for e in extensions if e.get("source") == "marketplace"] == []
    assert next(e for e in extensions if e["id"] == "jobs")["title"] == "Host jobs"
    assert second.client.get("/qcli/modules/tickets/extension.js").status_code == 404

    with open(os.path.join(first.dir, "installed.json"), "w", encoding="utf-8") as f:
        f.write("{ not json")
    third = start(net=net, dir=first.dir)
    assert third.get(f"{BASE}/installed").json() == []


def test_set_marketplace_false_serves_no_marketplace_routes_and_loads_nothing():
    result = create_cli_server()
    admin = CliAdminBuilder().set_credentials("admin", "pass").set_jwt_secret("x" * 32).set_marketplace(False).build(
        AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder),
    )
    try:
        admin.log_buffer.uninstall_handler()
        result.mount_plugin(admin)
        assert admin.marketplace is None
        assert admin.asset_routes == []
        client = TestClient(result.app)
        token = client.post(f"{V1}/auth/login", json={"username": "admin", "password": "pass"}).json()["token"]
        assert client.get(BASE, headers={"Authorization": f"Bearer {token}"}).status_code == 404
        assert not any(p.command == "dashboard" for p in result.registry.find_processor("admin").processors)
    finally:
        admin.log_buffer.uninstall_handler()


def test_refuses_an_invalid_marketplace_url_or_policy_when_built():
    result = create_cli_server()

    def build(options: dict[str, Any]) -> None:
        admin = CliAdminBuilder().set_credentials("admin", "pass").set_marketplace(options).build(
            AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder),
        )
        admin.log_buffer.uninstall_handler()

    with pytest.raises(ValueError, match="invalid marketplace url"):
        build({"url": "ftp://x"})
    with pytest.raises(ValueError, match="invalid marketplace install policy"):
        build({"install": "everyone"})


def test_the_default_directory_is_qcli_data_dashboard_plugins_below_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = create_cli_server()
    admin = CliAdminBuilder().set_credentials("admin", "pass").build(
        AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder),
    )
    admin.log_buffer.uninstall_handler()
    assert admin.marketplace is not None
    assert admin.marketplace.dir == os.path.join(str(tmp_path.resolve()), "qcli-data", "dashboard-plugins") or \
        os.path.realpath(admin.marketplace.dir) == os.path.realpath(os.path.join(tmp_path, "qcli-data", "dashboard-plugins"))
    assert admin.marketplace.info() == {"marketplaceUrl": "https://plugins.qodalis.com", "install": "verified", "dashboardApi": 1}


# --------------------------------------------------------------------------- protected server


def test_reading_needs_admin_access_changes_need_dashboard_install(start):
    h = start(users={
        "viewer": ["admin:access"], "installer": ["admin:access", "dashboard:install"], "outsider": ["files:read"],
    })
    h.net.publish(TICKETS, [{"version": "1.0.0"}])
    assert h.client.get(BASE).status_code == 401
    assert h.get(BASE, "outsider").status_code == 403
    assert h.get(f"{BASE}/plugins", "viewer").status_code == 200
    assert h.get(f"{BASE}/installed", "viewer").status_code == 200

    denied = h.post({"name": TICKETS}, "viewer")
    assert denied.status_code == 403
    assert "dashboard:install" in denied.text
    assert h.put(TICKETS, {}, "viewer").status_code == 403
    assert h.delete(TICKETS, "viewer").status_code == 403

    ok = h.post({"name": TICKETS}, "installer")
    assert ok.status_code == 201
    assert ok.json()["installedBy"] == "installer"
    assert h.delete(TICKETS, "root").status_code == 200
    # the users plugin's audit log has both
    audit = h.get(f"{V1}/audit?event=dashboard.plugin.install", "root")
    assert audit.status_code == 200
    assert "installer" in audit.text
    assert "dashboard.plugin.remove" in h.get(f"{V1}/audit?event=dashboard.plugin.remove", "root").text
    # dashboard:install is in the catalog
    catalog = h.get(f"{V1}/permissions", "root").json()
    assert {
        "name": "dashboard:install", "description": "Install, update and remove dashboard plugins from the marketplace",
        "group": "Server", "surface": "cli",
    } in catalog


def test_the_files_of_installed_plugins_are_served_without_sign_in(start):
    h = start(users={})
    h.net.publish(TICKETS, [{"version": "1.0.0"}])
    assert h.post({"name": TICKETS}, "root").status_code == 201
    assert h.client.get("/qcli/modules/tickets/extension.js").status_code == 200


def test_the_command_install_update_remove_need_dashboard_install_list_and_search_admin_access(start):
    h = start(users={
        "viewer": ["admin:access", "commands:execute"],
        "installer": ["admin:access", "commands:execute", "dashboard:install"],
    })
    h.net.publish(TICKETS, [{"version": "1.0.0"}])
    dashboard = next(p for p in h.result.registry.find_processor("admin").processors if p.command == "dashboard")
    assert {p.command: p.required_permissions for p in dashboard.processors} == {
        "list": None, "search": None, "install": ["dashboard:install"], "update": ["dashboard:install"], "remove": ["dashboard:install"],
    }
    assert h.run("admin dashboard search", "viewer").status == 200
    assert h.run(f"admin dashboard install {TICKETS}", "viewer").status == 403
    installed = h.run(f"admin dashboard install {TICKETS}", "installer")
    assert installed.text == f'Installed {TICKETS}@1.0.0: the dashboard page "cli-dashboard-tickets" (/x/tickets).'
    assert h.get(f"{BASE}/installed", "root").json()[0]["installedBy"] == "installer"


# --------------------------------------------------------------------------- the admin dashboard command


def test_command_list_search_install_name_at_version_update_remove(start):
    net = FakeNet()
    net.publish(TICKETS, [{"version": "1.0.0"}, {"version": "1.1.0"}], title="Tickets",
                server_packages={"python": "acme-cli-server-plugin-tickets"})
    net.publish("@other/cli-dashboard-crm", [{"version": "0.1.0"}], verified=False)
    h = start(net=net)

    def run(line: str) -> str:
        return h.run(line).text

    assert run("admin dashboard") == (
        f"No dashboard plugins installed (marketplace {MARKET}, install: verified; admin dashboard search <query> finds some)."
    )
    assert run("admin dashboard search") == "\n".join([
        "Package                      Version  Publisher         Status",
        f"{TICKETS}  1.1.0    @acme (verified)  installable; needs server plugin acme-cli-server-plugin-tickets",
        "@other/cli-dashboard-crm     0.1.0    @other            blocked: unverified publisher",
    ])
    assert run("admin dashboard search nothing-like-this") == 'No dashboard plugins match "nothing-like-this".'

    assert run(f"admin dashboard install {TICKETS}@1.0.0") == f'Installed {TICKETS}@1.0.0: the dashboard page "Tickets" (/x/tickets).'
    assert run(f"admin dashboard install {TICKETS}") == f"Error: already installed: {TICKETS}@1.0.0"
    assert run("admin dashboard install @other/cli-dashboard-crm") == "Error: publisher not verified"
    assert run("admin dashboard install ../../etc") == "Error: invalid package name"
    assert run("admin dashboard install") == "Error: name one package: admin dashboard install <name>[@version]"

    lines = run("admin dashboard list").split("\n")
    assert lines[0] == f"1 dashboard plugin(s) from {MARKET} (install: verified)"
    assert lines[1] == "ID       Package                      Version                   Installed            By"
    assert re.fullmatch(
        rf"tickets  {re.escape(TICKETS)}  1\.0\.0 \(update available\)  \d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d  anonymous", lines[2],
    )
    listed = json.loads(run("admin dashboard --json"))
    assert len(listed) == 1
    assert (listed[0]["name"], listed[0]["version"], listed[0]["updateAvailable"]) == (TICKETS, "1.0.0", True)
    assert "installed 1.0.0, update available" in run("admin dashboard search tickets")

    assert run(f"admin dashboard update {TICKETS}") == f"Updated {TICKETS} from 1.0.0 to 1.1.0."
    assert run(f"admin dashboard update {TICKETS}") == f"{TICKETS} is already at 1.1.0, the newest version."
    assert run("admin dashboard update @acme/none") == "Error: not installed: @acme/none"
    assert run(f"admin dashboard remove {TICKETS}") == f"Removed {TICKETS}@1.1.0 (the page tickets)."
    assert run(f"admin dashboard remove {TICKETS}") == f"Error: not installed: {TICKETS}"
    assert [(a.event, a.username) for a in h.audit] == [
        ("dashboard.plugin.install", "anonymous"),
        ("dashboard.plugin.update", "anonymous"),
        ("dashboard.plugin.remove", "anonymous"),
    ]


def test_command_reports_an_unreachable_marketplace(start):
    net = FakeNet()
    net.marketplace_down = True
    h = start(net=net)
    assert h.run("admin dashboard search").text == "Error: marketplace unreachable"
    assert h.run(f"admin dashboard install {TICKETS}").text == "Error: marketplace unreachable"
