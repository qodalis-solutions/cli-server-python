"""The migrations plugin on a server: API, route permissions, terminal commands, MCP tools, the run event, the
audit entry and the startup apply (SQLite)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_audit import InMemoryAuditStore
from qodalis_cli_migrations import (
    CliMigration,
    CliMigrationDatabaseOptions,
    CliMigrationsBuilder,
    sqlite_connection,
)
from qodalis_cli_users import CliUsersBuilder

API = "/api/v1/qcli/migrations"
MCP = "/api/qcli/mcp"
ROOT_PASS = "rootpass1"

MIGRATIONS = [
    CliMigration("1", "create notes", 'CREATE TABLE "Notes" ("Id" INTEGER PRIMARY KEY)', 'DROP TABLE "Notes"'),
    CliMigration("2", "create tags", 'CREATE TABLE "Tags" ("Id" INTEGER PRIMARY KEY)', 'DROP TABLE "Tags"'),
]


def _server(apply_on_startup: bool = False, migrations: list[CliMigration] | None = None, mcp: bool = False):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    result = create_cli_server(CliServerOptions(mcp=True if mcp else None))
    audit = InMemoryAuditStore()
    plugin = (
        CliMigrationsBuilder()
        .add_database("main", CliMigrationDatabaseOptions(sqlite_connection(db), apply_on_startup=apply_on_startup))
        .add_migrations("main", "app", migrations if migrations is not None else MIGRATIONS)
        .set_audit_store(audit)
        .build()
    )
    return result, plugin, audit


def _execute(client: TestClient, line: str, headers: dict[str, str] | None = None) -> str:
    """Runs ``migrations <sub> <db> --args`` as the web terminal sends it (already parsed)."""
    words = line.split()[1:]
    chain: list[str] = []
    args: dict[str, Any] = {}
    positionals: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("--"):
            nxt = words[i + 1] if i + 1 < len(words) else None
            if nxt is not None and not nxt.startswith("--"):
                args[w[2:]] = nxt
                i += 1
            else:
                args[w[2:]] = True
        elif not positionals and not chain and w in ("status", "up", "down"):
            chain.append(w)
        else:
            positionals.append(w)
        i += 1
    res = client.post("/api/v1/qcli/execute", headers=headers or {}, json={
        "command": "migrations", "chainCommands": chain, "value": " ".join(positionals) or None,
        "positionals": positionals, "args": args, "rawCommand": line,
    })
    return json.dumps(res.json())


def test_lists_shows_runs_and_rolls_back_through_the_api_publishing_and_auditing():
    result, plugin, audit = _server()
    events: list[tuple[str, dict]] = []
    result.event_bus.subscribe("migrations:*", lambda e: events.append((e.type, e.data)))
    result.mount_plugin(plugin)
    with TestClient(result.app) as c:
        assert c.get(API).json() == {"databases": [
            {"name": "main", "dialect": "sqlite", "sources": ["app"], "applied": 0, "pending": 2, "problems": 0},
        ]}
        detail = c.get(f"{API}/main").json()
        assert [f"{m['id']}:{m['status']}" for m in detail["migrations"]] == ["1:pending", "2:pending"]

        sql = c.get(f"{API}/main/sql", params={"to": "1"}).json()["sql"]
        assert 'CREATE TABLE "Notes"' in sql and '"Tags"' not in sql

        up = c.post(f"{API}/main/up", json={})
        assert up.status_code == 200
        body = up.json()
        assert (body["direction"], body["status"], body["trigger"]) == ("up", "succeeded", "api")
        assert [a["id"] for a in body["applied"]] == ["1", "2"]
        assert [(t, d["database"], d["direction"], d["status"], d["count"]) for t, d in events] == [
            ("migrations:run", "main", "up", "succeeded", 2),
        ]
        entries = asyncio.run(audit.get_entries())
        assert (entries[0].event, entries[0].target, entries[0].details) == ("migrations.up", "database:main", "app:1, app:2")

        down = c.post(f"{API}/main/down", json={"to": "1"}).json()
        assert [a["id"] for a in down["applied"]] == ["2"]
        assert c.post(f"{API}/main/down", json={}).json() == {
            "error": "to is required to roll back (an id, or base for everything)",
        }
        runs = c.get(f"{API}/main/runs").json()
        assert runs["total"] == 2 and runs["items"][0]["direction"] == "down"
        assert c.get(f"{API}/nope").status_code == 404
        assert c.post(f"{API}/main/up", json={"to": "x" * 300}).status_code == 400


def test_brings_its_commands_permissions_and_installed_plugin_entry():
    result, plugin, _ = _server()
    result.mount_plugin(plugin)
    names = [p.name for p in result.builder.permissions.all()]
    assert "migrations:read" in names and "migrations:run" in names
    assert "qodalis-cli-server-plugin-migrations" in [p.id for p in result.builder.installed_plugins]
    with TestClient(result.app) as c:
        assert "main (sqlite): 0 applied, 2 pending" in _execute(c, "migrations")
        assert "CREATE TABLE" in _execute(c, "migrations up --dry-run")
        assert "Applied 1 migration(s) (succeeded)" in _execute(c, "migrations up main --to 1")
        assert "applied  app:1  create notes" in _execute(c, "migrations status main")
        assert "--to is required" in _execute(c, "migrations down main")
        assert "Rolled back 1 migration(s)" in _execute(c, "migrations down main --to base")


def test_on_a_protected_server_reading_needs_read_and_running_needs_run():
    result, plugin, _ = _server()
    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    result.mount_plugin(users)
    result.mount_plugin(plugin)
    with TestClient(result.app) as c:
        def login(name: str, password: str) -> dict[str, str]:
            res = c.post("/api/v1/qcli/sessions", json={"username": name, "password": password})
            assert res.status_code == 201, res.text
            return {"Authorization": f"Bearer {res.json()['token']}"}

        root = login("root", ROOT_PASS)
        assert c.post("/api/v1/qcli/groups", headers=root, json={"name": "viewers", "permissions": ["migrations:read"]}).status_code == 201
        assert c.post("/api/v1/qcli/users", headers=root, json={
            "name": "viewer", "email": "v@example.com", "password": "viewer-pass-123", "groups": ["viewers", "users"],
        }).status_code == 201
        viewer = login("viewer", "viewer-pass-123")

        assert c.get(API).status_code == 401
        assert c.get(API, headers=viewer).status_code == 200
        refused = c.post(f"{API}/main/up", headers=viewer, json={})
        assert refused.status_code == 403 and refused.json() == {"error": "missing permission: migrations:run"}
        up = c.post(f"{API}/main/up", headers=root, json={}).json()
        assert (up["status"], up["actor"]) == ("succeeded", "root")
        assert "Error" not in _execute(c, "migrations status main", headers=viewer)
        assert "Error" not in _execute(c, "migrations", headers=viewer)


def test_serves_the_migrations_mcp_tools():
    result, plugin, _ = _server(mcp=True)
    result.mount_plugin(plugin)
    with TestClient(result.app) as c:
        ids = iter(range(1, 100))

        def rpc(method: str, params: Any = None) -> dict:
            body: dict[str, Any] = {"jsonrpc": "2.0", "id": next(ids), "method": method}
            if params is not None:
                body["params"] = params
            res = c.post(MCP, json=body, headers={"Accept": "application/json, text/event-stream"})
            assert res.status_code == 200, res.text
            return res.json()["result"]

        def call(name: str, args: dict | None = None) -> dict:
            return rpc("tools/call", {"name": name, "arguments": args or {}})

        tools = {t["name"] for t in rpc("tools/list")["tools"]}
        assert {"migrations_status", "migrations_runs", "migrations_sql", "migrations_up", "migrations_down"} <= tools
        assert call("migrations_status")["content"][0]["text"] == "main (sqlite): 0 applied, 2 pending"
        assert 'CREATE TABLE "Notes"' in call("migrations_sql", {"database": "main", "to": "1"})["content"][0]["text"]
        up = call("migrations_up", {"database": "main"})
        assert up["content"][0]["text"] == "Applied 2 migration(s) (succeeded)"
        assert (up["structuredContent"]["trigger"], up["structuredContent"]["status"]) == ("mcp", "succeeded")
        assert "applied  app:2 create tags" in call("migrations_status", {"database": "main"})["content"][0]["text"]
        down = call("migrations_down", {"database": "main", "to": "base"})
        assert down["content"][0]["text"] == "Rolled back 2 migration(s) (succeeded)"
        assert call("migrations_runs", {"database": "main"})["structuredContent"]["total"] == 2
        unknown = call("migrations_status", {"database": "nope"})
        assert unknown["isError"] is True and unknown["content"][0]["text"] == "unknown database: nope"


async def test_applies_on_startup_and_stops_with_the_failing_migration():
    _, ok, _ = _server(apply_on_startup=True)
    runs = await ok.apply_on_startup()
    assert [(r.trigger, len(r.applied)) for r in runs] == [("startup", 2)]

    _, bad, _ = _server(apply_on_startup=True, migrations=[CliMigration("1", "broken", "CREATE TABLE")])
    with pytest.raises(RuntimeError, match=r"migrations of main failed: app:1 broken: "):
        await bad.apply_on_startup()


def test_route_permissions_must_be_under_the_plugin_prefix():
    result, plugin, _ = _server()
    from qodalis_cli_server_abstractions import CliRoutePermission

    plugin.route_permissions = [CliRoutePermission("/api/v1/qcli/users", "migrations:read")]
    with pytest.raises(ValueError, match="invalid route permission: /api/v1/qcli/users"):
        result.mount_plugin(plugin)
