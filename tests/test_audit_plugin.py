"""The audit plugin (``qodalis_cli_audit``): its store, its API at
``/api/v1/qcli/audit``, on its own and added by the users plugin."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_audit import (
    DEFAULT_AUDIT_LIMIT,
    MAX_AUDIT_LIMIT,
    CliAuditBuilder,
    InMemoryAuditStore,
    get_audit_plugin,
)
from qodalis_cli_server_abstractions import AuditEntry as AbstractionsAuditEntry
from qodalis_cli_server_abstractions.audit import AuditEntry, AuditQueryOptions, IAuditStore
from qodalis_cli_users import CliUsersBuilder
from qodalis_cli_users.server_protection import http_permission

BASE = "/api/v1/qcli"
AUDIT = BASE + "/audit"
ROOT_PASS = "rootpass1"
AUDIT_DIST = "qodalis-cli-server-plugin-audit"
USERS_DIST = "qodalis-cli-server-plugin-users"


def _h(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def assert_error(res, status: int) -> None:
    assert res.status_code == status, res.text
    body = res.json()
    assert set(body) == {"error"} and body["error"], body


def _ids(result) -> list[str]:
    return [p.id for p in result.builder.installed_plugins]


def _audit_routes(result) -> list[tuple[str, frozenset]]:
    return [
        (r.path, frozenset(r.methods))
        for r in result.app.routes
        if getattr(r, "path", "").startswith(AUDIT)
    ]


def _users(store: IAuditStore | None = None, protect: bool = True):
    builder = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000)
    if store is not None:
        builder.set_audit_store(store)
    return builder.set_protect_server(protect).build()


def _login(client: TestClient, name: str = "root", password: str = ROOT_PASS) -> str:
    res = client.post(BASE + "/sessions", json={"username": name, "password": password})
    assert res.status_code == 201, res.text
    return res.json()["token"]


def _user_with(client: TestClient, root: str, name: str, perms: list[str]) -> str:
    assert client.post(BASE + "/groups", headers=_h(root),
                       json={"name": f"{name}-g", "permissions": perms}).status_code == 201
    assert client.post(BASE + "/users", headers=_h(root), json={
        "name": name, "email": f"{name}@example.com", "password": "pass1234", "groups": [f"{name}-g"],
    }).status_code == 201
    return _login(client, name, "pass1234")


def _entries(store: IAuditStore, **query: Any) -> list[AuditEntry]:
    return asyncio.run(store.get_entries(AuditQueryOptions(**query)))


# =========================================================================
# Contract and store
# =========================================================================


class TestContract:
    def test_the_contract_is_in_the_abstractions_and_re_exported(self) -> None:
        import qodalis_cli_users
        from qodalis_cli_users.models import AuditEntry as UsersModelsEntry
        from qodalis_cli_users.stores import IAuditStore as UsersStoreInterface
        from qodalis_cli_users.stores.in_memory_audit_store import InMemoryAuditStore as UsersInMemory

        assert AbstractionsAuditEntry is AuditEntry
        assert qodalis_cli_users.AuditEntry is AuditEntry
        assert UsersModelsEntry is AuditEntry
        assert qodalis_cli_users.AuditQueryOptions is AuditQueryOptions
        assert qodalis_cli_users.IAuditStore is IAuditStore
        assert UsersStoreInterface is IAuditStore
        assert qodalis_cli_users.InMemoryAuditStore is InMemoryAuditStore
        assert UsersInMemory is InMemoryAuditStore

    def test_entry_shape(self) -> None:
        entry = AuditEntry(event="role.save", actor="ann", target="role:editor",
                           properties={"permissions": ["jobs.view"]},
                           metadata={"remoteAddress": "10.0.0.1"})
        body = entry.to_dict()
        assert body["event"] == "role.save" and body["actor"] == "ann"
        assert body["target"] == "role:editor"
        assert body["properties"] == {"permissions": ["jobs.view"]}
        assert body["metadata"] == {"remoteAddress": "10.0.0.1"}
        assert "userId" not in body and "details" not in body


class TestInMemoryStore:
    async def test_failed_logins_are_capped_apart(self) -> None:
        store = InMemoryAuditStore(max_entries=5, max_failed_login_entries=2)
        for i in range(4):
            await store.log(AuditEntry(event="user_created", username=f"u{i}", timestamp=i))
        for i in range(10):
            await store.log(AuditEntry(event="login_failed", username="x", timestamp=100 + i))

        entries = await store.get_entries()
        assert [e.event for e in entries].count("login_failed") == 2
        assert [e.username for e in entries if e.event == "user_created"] == ["u3", "u2", "u1", "u0"]

    async def test_newest_recorded_first(self) -> None:
        store = InMemoryAuditStore()
        await store.log(AuditEntry(event="a"))
        await store.log(AuditEntry(event="login_failed"))
        await store.log(AuditEntry(event="c"))
        assert [e.event for e in await store.get_entries()] == ["c", "login_failed", "a"]

    async def test_a_limit_of_zero_returns_nothing(self) -> None:
        store = InMemoryAuditStore()
        await store.log(AuditEntry(event="a"))
        assert await store.get_entries(AuditQueryOptions(limit=0)) == []


# =========================================================================
# The plugin on its own
# =========================================================================


class TestPluginAlone:
    def test_serves_the_log_without_the_users_plugin(self) -> None:
        result = create_cli_server(CliServerOptions())
        audit = CliAuditBuilder().build()
        result.mount_plugin(audit)
        assert get_audit_plugin(result) is audit
        assert _ids(result)[-1] == AUDIT_DIST

        with TestClient(result.app) as client:
            assert client.get(AUDIT).json() == []
            client.portal.call(audit.store.log, AuditEntry(event="role.save", actor="ann", target="role:x"))
            body = client.get(AUDIT).json()
            assert [e["event"] for e in body] == ["role.save"]

            assert client.delete(AUDIT).status_code == 204
            cleared = client.get(AUDIT).json()
            assert [e["event"] for e in cleared] == ["audit_cleared"]
            # No auth plugin: nobody is known.
            assert cleared[0]["username"] == "" and "actor" not in cleared[0]

    def test_mounting_again_adds_nothing(self) -> None:
        result = create_cli_server(CliServerOptions())
        audit = CliAuditBuilder().build()
        result.mount_plugin(audit)
        routes = _audit_routes(result)
        result.mount_plugin(audit)
        result.mount_plugin(CliAuditBuilder().build())

        assert _audit_routes(result) == routes
        assert _ids(result).count(AUDIT_DIST) == 1
        assert get_audit_plugin(result) is audit

    def test_listed_with_its_package_metadata(self) -> None:
        result = create_cli_server(CliServerOptions())
        result.mount_plugin(CliAuditBuilder().build())
        plugin = next(p for p in result.builder.installed_plugins if p.id == AUDIT_DIST)
        assert plugin.name == "Audit"
        assert plugin.version
        assert "Audit log" in (plugin.description or "")


# =========================================================================
# Added by the users plugin; one store
# =========================================================================


class TestAddedByUsers:
    def test_users_alone_adds_the_audit_plugin(self) -> None:
        result = create_cli_server(CliServerOptions())
        users = _users()
        result.mount_plugin(users)

        assert _ids(result)[-2:] == [USERS_DIST, AUDIT_DIST]
        assert get_audit_plugin(result) is users.audit_plugin
        with TestClient(result.app) as client:
            root = _login(client)
            events = [e["event"] for e in client.get(AUDIT, headers=_h(root)).json()]
            assert "login" in events

    def test_a_store_set_on_the_users_plugin_is_kept(self) -> None:
        store = InMemoryAuditStore()
        result = create_cli_server(CliServerOptions())
        users = _users(store)
        result.mount_plugin(users)
        # The host's own audit plugin (default store) mounted after it.
        result.mount_plugin(CliAuditBuilder().build())

        assert users.audit_plugin.store is store
        assert users.auth_service.audit_store is store
        with TestClient(result.app) as client:
            root = _login(client)
            assert [e.event for e in _entries(store, event="login")] == ["login"]
            assert client.get(AUDIT + "?event=login", headers=_h(root)).json()[0]["username"] == "root"

    def test_a_store_set_on_an_audit_plugin_mounted_first_is_kept(self) -> None:
        store = InMemoryAuditStore()
        result = create_cli_server(CliServerOptions())
        audit = CliAuditBuilder().set_store(store).build()
        result.mount_plugin(audit)
        users = _users()
        result.mount_plugin(users)

        assert get_audit_plugin(result) is audit
        assert users.auth_service.audit_store is store
        assert users.audit_store is store
        assert _ids(result).count(AUDIT_DIST) == 1
        with TestClient(result.app) as client:
            root = _login(client)
            assert _entries(store, event="login")
            assert client.get(AUDIT + "?event=login", headers=_h(root)).json()

    def test_a_store_set_on_an_audit_plugin_mounted_after_is_kept(self) -> None:
        store = InMemoryAuditStore()
        result = create_cli_server(CliServerOptions())
        users = _users()
        result.mount_plugin(users)
        routes = _audit_routes(result)
        result.mount_plugin(CliAuditBuilder().set_store(store).build())

        assert _audit_routes(result) == routes
        assert users.auth_service.audit_store is store
        assert users.audit_plugin.store is store
        with TestClient(result.app) as client:
            root = _login(client)
            assert _entries(store, event="login")
            assert client.get(AUDIT + "?event=login", headers=_h(root)).json()


# =========================================================================
# API
# =========================================================================


@pytest.fixture()
def srv():
    result = create_cli_server(CliServerOptions())
    users = _users()
    result.mount_plugin(users)
    with TestClient(result.app) as client:
        yield client, users, _login(client)


class TestApi:
    def test_default_and_maximum_limit(self, srv) -> None:
        client, users, root = srv
        for i in range(MAX_AUDIT_LIMIT + 50):
            client.portal.call(users.audit_plugin.store.log, AuditEntry(event="note", username=f"u{i}", timestamp=i))

        assert len(client.get(AUDIT, headers=_h(root)).json()) == DEFAULT_AUDIT_LIMIT
        assert len(client.get(AUDIT + "?limit=5000", headers=_h(root)).json()) == MAX_AUDIT_LIMIT
        assert len(client.get(AUDIT + "?limit=3", headers=_h(root)).json()) == 3
        assert client.get(AUDIT + "?limit=0", headers=_h(root)).json() == []

    def test_filters_are_exact_and_case_insensitive(self, srv) -> None:
        client, users, root = srv
        store = users.audit_plugin.store
        for entry in (
            AuditEntry(event="role.save", username="Bob", actor="Ann"),
            AuditEntry(event="role.save", username="bobby", actor="ann"),
            AuditEntry(event="role.delete", username="bob", actor="carl"),
        ):
            client.portal.call(store.log, entry)

        def events(query: str) -> list[str]:
            return [e["username"] for e in client.get(AUDIT + query, headers=_h(root)).json()]

        assert events("?event=ROLE.SAVE") == ["bobby", "Bob"]
        assert events("?username=BOB") == ["bob", "Bob"]
        assert events("?actor=ANN&username=bob") == ["Bob"]
        assert events("?event=role") == []
        page = client.get(AUDIT + "?event=role.save&offset=1&limit=1", headers=_h(root)).json()
        assert [e["username"] for e in page] == ["Bob"]

    @pytest.mark.parametrize("query", ["limit=x", "limit=-1", "offset=-1", "offset=1.5", "limit=1e3"])
    def test_bad_paging_is_400(self, srv, query: str) -> None:
        client, _, root = srv
        assert_error(client.get(AUDIT + "?" + query, headers=_h(root)), 400)

    def test_clear_records_who_cleared_it(self, srv) -> None:
        client, users, root = srv
        res = client.delete(AUDIT, headers=_h(root))
        assert res.status_code == 204
        entries = client.get(AUDIT, headers=_h(root)).json()
        assert [e["event"] for e in entries] == ["audit_cleared"]
        cleared = entries[0]
        assert cleared["actor"] == "root" and cleared["username"] == "root"
        assert cleared["userId"] and cleared["actorId"] == cleared["userId"]
        assert cleared["details"] == "audit log cleared"
        assert "remoteAddress" in cleared["metadata"]

    def test_with_and_without_trailing_slash(self, srv) -> None:
        client, _, root = srv
        for path in (AUDIT, AUDIT + "/"):
            res = client.get(path, headers=_h(root), follow_redirects=False)
            assert res.status_code == 200 and isinstance(res.json(), list)


# =========================================================================
# Permissions
# =========================================================================


class TestPermissions:
    def test_route_policy(self) -> None:
        assert http_permission(AUDIT, "GET") == "audit:read"
        assert http_permission(AUDIT + "/", "HEAD") == "audit:read"
        assert http_permission(AUDIT, "DELETE") == "audit:clear"
        assert http_permission(BASE + "/auditing", "GET") is None

    @pytest.mark.parametrize("protect", [True, False])
    def test_read_and_clear_need_their_permissions(self, protect: bool) -> None:
        result = create_cli_server(CliServerOptions())
        result.mount_plugin(_users(protect=protect))
        with TestClient(result.app) as client:
            root = _login(client)
            assert_error(client.get(AUDIT), 401)
            assert_error(client.delete(AUDIT), 401)

            reader = _user_with(client, root, "reader", ["audit:read"])
            assert client.get(AUDIT, headers=_h(reader)).status_code == 200
            assert_error(client.delete(AUDIT, headers=_h(reader)), 403)

            nobody = _user_with(client, root, "nobody", ["users:read"])
            assert_error(client.get(AUDIT, headers=_h(nobody)), 403)

            clearer = _user_with(client, root, "clearer", ["audit:clear"])
            assert_error(client.get(AUDIT, headers=_h(clearer)), 403)
            assert client.delete(AUDIT, headers=_h(clearer)).status_code == 204
            assert client.get(AUDIT, headers=_h(root)).json()[0]["actor"] == "clearer"
