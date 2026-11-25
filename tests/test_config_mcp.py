"""MCP tools config_list, config_get (read-only, config:read) and config_set
(config:write, not read-only: hidden and refused in read-only mode), secrets
never returned.  Port of the Node ``config-mcp.test.ts``."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliMcpOptions, CliServerOptions, create_cli_server
from qodalis_cli_server_abstractions.audit import AuditQueryOptions
from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
)
from qodalis_cli_users import CliUsersBuilder

MCP = "/api/qcli/mcp"
V1 = "/api/v1/qcli"
ROOT_PASS = "rootpass1"


def app_provider() -> CliStoredConfigProvider:
    return CliStoredConfigProvider(
        id="app",
        title="App",
        sections=[
            CliConfigSectionDefinition(id="main", settings=[
                CliConfigSettingDefinition(
                    key="limit", type="number", default=5, validation=CliConfigValidation(min=1, max=10),
                ),
                CliConfigSettingDefinition(key="apiKey", type="secret"),
            ]),
        ],
    )


class Rpc:
    def __init__(self, client: TestClient) -> None:
        self.c = client
        self._id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
        self._id += 1
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        res = self.c.post(MCP, headers=headers, json={
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {},
        })
        assert res.status_code == 200, res.text
        return res.json()

    def call(self, name: str, args: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
        return self.rpc("tools/call", {"name": name, "arguments": args or {}}, token)

    def tool_names(self, token: str | None = None) -> list[str]:
        return [t["name"] for t in self.rpc("tools/list", {}, token)["result"]["tools"]]


# ---------------------------------------------------------------------------
# Open server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cli():
    result = create_cli_server(CliServerOptions(mcp=True, configure=lambda b: b.add_config_provider(app_provider())))
    with TestClient(result.app) as client:
        yield Rpc(client)


class TestConfigMcpOpenServer:
    def test_lists_the_three_tools_with_schemas_and_read_only_hints(self, cli: Rpc):
        tools = cli.rpc("tools/list")["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert by_name["config_list"]["annotations"] == {"readOnlyHint": True}
        assert by_name["config_get"]["annotations"] == {"readOnlyHint": True}
        assert by_name["config_set"]["annotations"]["readOnlyHint"] is False
        assert by_name["config_get"]["inputSchema"]["required"] == ["provider"]
        assert by_name["config_set"]["inputSchema"]["required"] == ["provider", "section", "changes"]
        assert by_name["config_set"]["inputSchema"]["properties"]["changes"]["type"] == "object"
        # The config command is not also exposed as command tools
        assert [t["name"] for t in tools if t["name"].startswith("config")] == [
            "config_list", "config_get", "config_set",
        ]

    def test_config_list_the_store_providers_with_their_storage_and_section_summaries(self, cli: Rpc):
        res = cli.call("config_list")
        assert res["result"]["isError"] is False
        assert res["result"]["structuredContent"]["store"] == {
            "kind": "memory", "title": "In memory", "detail": "", "persistent": False,
        }
        providers = res["result"]["structuredContent"]["providers"]
        assert [p["id"] for p in providers] == ["server", "host", "cors", "mcp", "app", "custom"]
        assert next(p for p in providers if p["id"] == "app") == {
            "id": "app",
            "title": "App",
            "description": "",
            "writable": True,
            "storage": {"kind": "store", "description": "Changes are saved to the config store", "sources": []},
            "sections": [{"id": "main", "title": "main", "mutable": True, "version": "0", "keys": 2}],
        }

    def test_config_get_provider_section_entry_unknown_ones_are_errors(self, cli: Rpc):
        provider = cli.call("config_get", {"provider": "app"})["result"]["structuredContent"]["provider"]
        assert provider["id"] == "app"
        section = cli.call("config_get", {"provider": "app", "section": "main"})["result"]["structuredContent"]
        assert [e["key"] for e in section["section"]["entries"]] == ["limit", "apiKey"]
        entry = cli.call("config_get", {"provider": "app", "section": "main", "key": "limit"})["result"][
            "structuredContent"
        ]
        assert entry["provider"] == "app"
        assert entry["section"] == "main"
        assert entry["version"] == "0"
        assert entry["entry"]["key"] == "limit"
        assert entry["entry"]["value"] == 5

        unknown_provider = cli.call("config_get", {"provider": "nope"})
        assert unknown_provider["result"]["isError"] is True
        assert unknown_provider["result"]["structuredContent"] == {"error": "unknown config provider: nope"}

        unknown_section = cli.call("config_get", {"provider": "app", "section": "nope"})["result"]
        assert unknown_section["isError"] is True
        assert unknown_section["content"][0]["text"] == "unknown config section: app.nope"
        assert cli.call("config_get", {"provider": "app", "section": "main", "key": "nope"})["result"]["isError"] is True
        missing = cli.rpc("tools/call", {"name": "config_get", "arguments": {}})
        assert "missing required argument 'provider'" in missing["error"]["message"]

    def test_config_set_applies_per_key_errors_conflicts_never_returns_secrets(self, cli: Rpc):
        ok = cli.call("config_set", {
            "provider": "app", "section": "main", "changes": {"limit": 8, "apiKey": "sk_mcp_secret"}, "version": "0",
        })
        assert ok["result"]["isError"] is False
        sc = ok["result"]["structuredContent"]
        assert sc["provider"] == "app"
        assert sc["applied"] == ["limit", "apiKey"]
        assert sc["version"] == "1"
        assert "sk_mcp_secret" not in json.dumps(ok)

        invalid = cli.call("config_set", {"provider": "app", "section": "main", "changes": {"limit": 50}})
        assert invalid["result"]["isError"] is True
        assert invalid["result"]["structuredContent"] == {
            "error": "validation failed", "errors": {"limit": "must be at most 10"},
        }

        stale = cli.call("config_set", {"provider": "app", "section": "main", "changes": {"limit": 2}, "version": "0"})
        assert stale["result"]["structuredContent"] == {"error": "version conflict", "version": "1"}

        got = cli.call("config_get", {"provider": "app", "section": "main", "key": "apiKey"})
        assert got["result"]["structuredContent"]["entry"]["value"] is None
        assert got["result"]["structuredContent"]["entry"]["isSet"] is True
        assert "sk_mcp_secret" not in json.dumps(got)

        bad = cli.rpc("tools/call", {
            "name": "config_set", "arguments": {"provider": "app", "section": "main", "changes": "limit=1"},
        })
        assert "argument 'changes' must be an object" in bad["error"]["message"]

    def test_config_set_is_refused_when_the_endpoint_is_read_only(self):
        result = create_cli_server(CliServerOptions(
            mcp=CliMcpOptions(read_only=True), configure=lambda b: b.add_config_provider(app_provider()),
        ))
        with TestClient(result.app) as client:
            ro = Rpc(client)
            names = ro.tool_names()
            assert "config_list" in names
            assert "config_get" in names
            assert "config_set" not in names
            refused = ro.call("config_set", {"provider": "app", "section": "main", "changes": {"limit": 2}})
            assert refused["error"] == {
                "code": -32602, "message": "Tool config_set is not available: the MCP endpoint is read-only",
            }
            got = ro.call("config_get", {"provider": "app", "section": "main", "key": "limit"})
            assert got["result"]["structuredContent"]["entry"]["value"] == 5

    def test_server_info_features_list_config(self, cli: Rpc):
        assert "config" in cli.call("server_info")["result"]["structuredContent"]["features"]


# ---------------------------------------------------------------------------
# Protected server (users plugin)
# ---------------------------------------------------------------------------


class _Protected:
    def __init__(self, rpc: Rpc, users: Any, tokens: dict[str, str]) -> None:
        self.rpc, self.users, self.tokens = rpc, users, tokens

    def config_tool_names(self, name: str) -> list[str]:
        return [n for n in self.rpc.tool_names(self.tokens[name]) if n.startswith("config")]


@pytest.fixture(scope="module")
def protected():
    def configure(b):
        b.add_config_provider(app_provider())
        b.add_config_provider(CliStoredConfigProvider(
            id="vault", title="Vault", required_permission="vault:admin",
            sections=[CliConfigSectionDefinition(id="s", settings=[CliConfigSettingDefinition(key="k", type="string")])],
        ))

    result = create_cli_server(CliServerOptions(mcp=True, configure=configure))
    users = (
        CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(10_000).set_min_password_length(4).build()
    )
    result.mount_plugin(users)
    tokens: dict[str, str] = {}
    with TestClient(result.app) as client:
        def login(username: str, password: str) -> str:
            res = client.post(f"{V1}/sessions", json={"username": username, "password": password})
            assert res.status_code == 201, res.text
            return res.json()["token"]

        tokens["root"] = login("root", ROOT_PASS)
        root = {"Authorization": f"Bearer {tokens['root']}"}

        def mk(name: str, permissions: list[str]) -> None:
            res = client.post(f"{V1}/groups", headers=root, json={"name": f"{name}s", "permissions": permissions})
            assert res.status_code == 201, res.text
            res = client.post(f"{V1}/users", headers=root, json={
                "name": name, "email": f"{name}@x.io", "password": f"{name}pass", "groups": [f"{name}s"],
            })
            assert res.status_code == 201, res.text
            tokens[name] = login(name, f"{name}pass")

        mk("runner", ["commands:execute"])
        mk("reader", ["commands:execute", "config:read"])
        mk("writer", ["commands:execute", "config:read", "config:write"])
        yield _Protected(Rpc(client), users, tokens)


class TestConfigMcpProtectedServer:
    def test_tools_follow_config_read_and_config_write(self, protected: _Protected):
        assert protected.config_tool_names("runner") == []
        assert protected.config_tool_names("reader") == ["config_list", "config_get"]
        assert protected.config_tool_names("writer") == ["config_list", "config_get", "config_set"]
        refused = protected.rpc.call(
            "config_set", {"provider": "app", "section": "main", "changes": {"limit": 2}}, protected.tokens["reader"],
        )
        assert refused["error"]["message"] == "Unknown tool: config_set"

    def test_a_provider_required_permission_hides_it_from_the_tools_too(self, protected: _Protected):
        writer, root = protected.tokens["writer"], protected.tokens["root"]
        ids = [p["id"] for p in protected.rpc.call("config_list", {}, writer)["result"]["structuredContent"]["providers"]]
        assert "vault" not in ids
        res = protected.rpc.call("config_get", {"provider": "vault"}, writer)["result"]
        assert res["isError"] is True
        assert res["structuredContent"] == {"error": "forbidden: missing permission vault:admin"}
        ids = [p["id"] for p in protected.rpc.call("config_list", {}, root)["result"]["structuredContent"]["providers"]]
        assert "vault" in ids

    def test_changes_are_audited_with_via_mcp_and_the_caller(self, protected: _Protected):
        res = protected.rpc.call(
            "config_set", {"provider": "app", "section": "main", "changes": {"limit": 3}}, protected.tokens["writer"],
        )
        assert res["result"]["isError"] is False, res
        entries = protected.rpc.c.portal.call(
            protected.users.audit_store.get_entries, AuditQueryOptions(event="config.update"),
        )
        entry = entries[0]
        assert entry.username == "writer"
        assert entry.target == "config:app.main"
        assert entry.properties["via"] == "mcp"
