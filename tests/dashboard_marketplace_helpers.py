"""The fake marketplace and CDN and the marketplace test servers shared by the dashboard marketplace tests (no
network)."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_admin import (
    AdminBuildDeps,
    CliAdminBuilder,
    DashboardMarketplaceOptions,
    MarketplaceResponse,
    compare_dashboard_plugin_versions,
)

from .command_helpers import V1, add_users, protect, run_command

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
        #: Answer details without the versions' ``config``: the version record has it.
        self.config_only_in_version_record = False

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
            if "config" in v:
                # The version's settings (``config`` of its record); absent: the record has none
                record["config"] = v["config"]
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
        latest_record = next((r for r in records if r["version"] == latest), None)
        if latest_record is not None and "config" in latest_record:
            plugin["config"] = latest_record["config"]
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
            if self.config_only_in_version_record:
                return response(200, {
                    **plugin, "versions": [{k: x for k, x in v.items() if k != "config"} for v in plugin["versions"]],
                })
            return response(200, plugin)
        return response(404, {"error": "not found"})


class Host:
    """A server with the admin plugin (password sign-in, or the users plugin) and the fake marketplace."""

    def __init__(
        self, tmp_path: Any, *, net: Optional[FakeNet] = None, dir: Optional[str] = None, install: Optional[str] = None,
        configure: Any = None, users: Optional[dict[str, list[str]]] = None, config: Any = None,
    ) -> None:
        self.net = net or FakeNet()
        self.dir = dir or str(tmp_path / f"plugins-{id(self)}")
        os.makedirs(self.dir, exist_ok=True)
        self.result = create_cli_server(CliServerOptions(config=config) if config is not None else None)
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
