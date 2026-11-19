"""A server without CLI routes (``CliServerOptions(cli_routes=False)``) and a
guard-only users plugin (``set_guard_only()``): a second service that only
validates the tokens of shared stores."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliEchoCommandProcessor, CliPermission, CliServerOptions, create_cli_server
from qodalis_cli_users import (
    ApiTokenCreateOptions,
    CliUsersBuilder,
    InMemoryApiTokenStore,
    InMemoryGroupStore,
    InMemoryUserStore,
)

from tests.test_users_host_tokens import HOST_TEXT, ROOT_PASS, _make

RATES = CliPermission(name="rates:read", description="Official exchange rates", group="Rates")


def _configure(b: Any) -> None:
    b.add_processor(CliEchoCommandProcessor())
    b.add_permissions([RATES])


class Pair:
    """A normal server (creates alice and her host token) and a guard-only
    server over the same user, group and token stores."""

    def __init__(self) -> None:
        self.user_store = InMemoryUserStore()
        self.group_store = InMemoryGroupStore()
        self.token_store = InMemoryApiTokenStore()

        self.normal = create_cli_server(CliServerOptions(configure=_configure))
        self.normal_users = self._builder().set_default_admin_password(ROOT_PASS).build()
        self.normal.mount_plugin(self.normal_users)

        self.guard = create_cli_server(CliServerOptions(configure=_configure, cli_routes=False))
        self.guard_users = self._builder().set_guard_only().build()
        self.guard.mount_plugin(self.guard_users)

        @self.guard.app.get("/host/rates")
        async def rates(caller=Depends(self.guard_users.require_host_token("rates:read"))):
            return {"user": caller.caller.user_name}

    def _builder(self) -> CliUsersBuilder:
        return (
            CliUsersBuilder()
            .set_user_store(self.user_store)
            .set_group_store(self.group_store)
            .set_api_token_store(self.token_store)
        )


@pytest.fixture()
def pair():
    p = Pair()
    with TestClient(p.normal.app) as normal, TestClient(p.guard.app) as guard:
        p.normal_client = normal
        p.guard_client = guard

        async def make() -> str:
            await p.normal_users.ensure_seeded()
            await p.group_store.create_group("alice-g", permissions=["rates:read"])
            alice = await p.user_store.create_user("alice", "alice@example.com", ["users", "alice-g"])
            created = await p.normal_users.api_tokens.create(
                alice.id, ApiTokenCreateOptions(name="site", audience="host", permissions=["rates:read"]),
            )
            return created.token

        p.host_token = normal.portal.call(make)
        yield p


def test_host_route_accepts_the_host_token(pair: Pair):
    res = pair.guard_client.get("/host/rates", headers={"Authorization": f"Bearer {pair.host_token}"})
    assert res.status_code == 200, res.text
    assert res.json() == {"user": "alice"}


def test_host_route_still_refuses_without_a_token(pair: Pair):
    res = pair.guard_client.get("/host/rates")
    assert (res.status_code, res.json()) == (401, {"error": "unauthorized"})


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/qcli/commands"),
    ("POST", "/api/v1/qcli/execute"),
    ("POST", "/api/v1/qcli/auth/login"),
    ("POST", "/api/v1/qcli/sessions"),
    ("GET", "/api/v1/qcli/tokens"),
    ("GET", "/api/v1/qcli/users"),
    ("GET", "/api/v1/qcli/permissions"),
    ("GET", "/api/v1/qcli/audit"),
    ("GET", "/api/qcli/versions"),
    ("GET", "/api/v1/qcli/version"),
    ("POST", "/api/qcli/execute"),
])
def test_cli_and_users_routes_answer_404(pair: Pair, method: str, path: str):
    for token in (None, pair.host_token):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        res = pair.guard_client.request(method, path, headers=headers, json={})
        assert res.status_code == 404, (path, res.text)


@pytest.mark.parametrize("path", ["/ws/qcli/events", "/ws/v1/qcli/logs", "/ws/qcli/shell"])
def test_sockets_are_not_served(pair: Pair, path: str):
    with pytest.raises(WebSocketDisconnect):
        with pair.guard_client.websocket_connect(path) as ws:
            ws.receive_text()


def test_no_seeding():
    users_store, groups_store = InMemoryUserStore(), InMemoryGroupStore()
    server = create_cli_server(CliServerOptions(configure=_configure, cli_routes=False))
    users = (
        CliUsersBuilder().set_user_store(users_store).set_group_store(groups_store)
        .set_guard_only().build()
    )
    server.mount_plugin(users)

    @server.app.get("/host/rates")
    async def rates(caller=Depends(users.require_host_token("rates:read"))):
        return {}

    with TestClient(server.app) as client:
        assert client.get("/host/rates").status_code == 401
        client.portal.call(users.startup)
        assert client.portal.call(users_store.get_users) == []
        assert client.portal.call(groups_store.get_groups) == []
        client.portal.call(users.shutdown)


def test_guard_only_adds_no_routes():
    users = CliUsersBuilder().set_guard_only().build()
    assert users.router.routes == []
    assert users.protect_server is False


def test_mount_plugin_of_a_cli_plugin_logs_and_skips(caplog):
    from qodalis_cli_jobs import CliJobsBuilder

    server = create_cli_server(CliServerOptions(cli_routes=False))
    with caplog.at_level(logging.INFO):
        server.mount_plugin(CliJobsBuilder().build())
    assert "[qcli] CLI routes are turned off; /api/v1/qcli/jobs is not mounted" in caplog.text
    with TestClient(server.app) as client:
        assert client.get("/api/v1/qcli/jobs").status_code == 404


def test_catalog_still_serves_host_entries(pair: Pair):
    assert pair.guard.builder.permissions.find("rates:read") is not None


def test_default_host_unchanged(pair: Pair):
    res = pair.normal_client.post("/api/v1/qcli/sessions", json={"username": "root", "password": ROOT_PASS})
    token = res.json()["token"]
    res = pair.normal_client.get("/api/v1/qcli/commands", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


def test_core_without_cli_routes():
    server = create_cli_server(CliServerOptions(cli_routes=False, mcp=True))
    with TestClient(server.app) as client:
        for path in ("/api/v1/qcli/commands", "/api/qcli/versions", "/api/v1/qcli/capabilities"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/qcli/mcp", json={}).status_code == 404


# ---------------------------------------------------------------------------
# Plugins serving host routes (cli_routes = False)
# ---------------------------------------------------------------------------


@dataclass
class _StubPlugin:
    prefix: str
    router: APIRouter
    cli_routes: bool = True


def _stub(cli_routes: bool) -> _StubPlugin:
    router = APIRouter()

    @router.get("/ping")
    async def ping():
        return {"pong": True}

    plugin = _StubPlugin(prefix="/qcli/x", router=router)
    if not cli_routes:
        plugin.cli_routes = False
    return plugin


@pytest.mark.parametrize("cli_routes", [True, False])
def test_a_cli_routes_false_plugin_prefix_is_not_a_cli_route(cli_routes: bool):
    result, users = _make(CliUsersBuilder().set_protect_server(False))
    result.mount_plugin(_stub(cli_routes))
    assert ("/qcli/x" in result.cli_route_prefixes) is cli_routes
    with TestClient(result.app) as client:
        async def make() -> str:
            await users.ensure_seeded()
            await users.group_store.create_group("alice-g", permissions=["rates:read"])
            alice = await users.user_store.create_user("alice", "alice@example.com", ["users", "alice-g"])
            created = await users.api_tokens.create(
                alice.id, ApiTokenCreateOptions(name="t", audience="host", permissions=["rates:read"]),
            )
            return created.token

        token = client.portal.call(make)
        res = client.get("/qcli/x/ping", headers={"Authorization": f"Bearer {token}"})
        if cli_routes:
            assert (res.status_code, res.json()) == (403, {"error": HOST_TEXT})
        else:
            assert (res.status_code, res.json()) == (200, {"pong": True})


def test_cli_routes_false_plugin_mounts_on_a_server_without_cli_routes():
    server = create_cli_server(CliServerOptions(cli_routes=False))
    server.mount_plugin(_stub(False))
    with TestClient(server.app) as client:
        assert client.get("/qcli/x/ping").json() == {"pong": True}


# ---------------------------------------------------------------------------
# Guard-only fails closed: it needs a server without CLI routes
# ---------------------------------------------------------------------------


def test_guard_only_with_cli_routes_on_fails_at_mount():
    server = create_cli_server(CliServerOptions(configure=_configure))
    users = CliUsersBuilder().set_guard_only().build()
    with pytest.raises(ValueError, match=r"^guard-only needs cli_routes=False$"):
        server.mount_plugin(users)


def test_guard_only_server_without_cli_routes_serves_no_mcp():
    server = create_cli_server(CliServerOptions(configure=_configure, cli_routes=False, mcp=True))
    users = CliUsersBuilder().set_guard_only().build()
    server.mount_plugin(users)
    with TestClient(server.app) as client:
        assert client.post("/api/qcli/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 404
        assert client.post("/api/v1/qcli/link", json={}).status_code == 404


async def test_guard_only_runs_no_sweeps():
    users = CliUsersBuilder().set_guard_only().build()
    await users.startup()
    try:
        assert users._cleanup_task is None
    finally:
        await users.shutdown()


def test_admin_dashboard_not_served_without_cli_routes(caplog, tmp_path):
    from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder

    (tmp_path / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
    options = CliServerOptions(cli_routes=False)
    server = create_cli_server(options)
    admin = CliAdminBuilder().set_credentials("admin", "secret-pass-1").set_dashboard_path(str(tmp_path)).build(
        AdminBuildDeps(
            registry=server.registry, event_socket_manager=server.event_socket_manager,
            builder=server.builder, server_options=options,
        ),
    )
    assert admin.dashboard_app is not None
    with caplog.at_level(logging.WARNING):
        server.mount_plugin(admin)
    assert "[qcli] the CLI routes are turned off (cli_routes=False); the admin dashboard is not served" in caplog.text
    with TestClient(server.app) as client:
        assert client.get("/qcli/admin/").status_code == 404
        assert client.post("/api/v1/qcli/auth/login", json={}).status_code == 404


def test_guard_only_never_mounted_answers_500(caplog):
    from fastapi import FastAPI

    users = CliUsersBuilder().set_guard_only().build()
    app = FastAPI()

    @app.get("/host/rates")
    async def rates(caller=Depends(users.require_host_token("rates:read"))):
        return {}

    @app.get("/host/me")
    async def me(ctx=Depends(users.require_auth)):
        return {}

    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        res = client.get("/host/rates", headers={"Authorization": "Bearer qcli_pat_x"})
        assert res.status_code == 500
        assert "internal server error" in res.text
        assert client.get("/host/me").status_code == 500
    assert "guard-only needs cli_routes=False" in caplog.text


def test_guard_only_after_a_failed_mount_still_answers_500():
    server = create_cli_server(CliServerOptions(configure=_configure))
    users = CliUsersBuilder().set_guard_only().build()
    with pytest.raises(ValueError):
        server.mount_plugin(users)

    @server.app.get("/host/rates")
    async def rates(caller=Depends(users.require_host_token("rates:read"))):
        return {}

    with TestClient(server.app) as client:
        assert client.get("/host/rates").status_code == 500
