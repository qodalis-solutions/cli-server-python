"""``host`` API tokens: accepted only by the host's guarded routes
(``require_auth`` / ``require_permission``), refused on every CLI route."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliEchoCommandProcessor, CliPermission, CliServerOptions, create_cli_server
from qodalis_cli_users import (
    ApiTokenCreateOptions,
    CliUsersBuilder,
    ExternalAuthResult,
    User,
)
from qodalis_cli_users.server_protection import is_cli_path

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
HOST_TEXT = "forbidden: host tokens cannot use CLI routes"

#: CLI routes a host token is refused on (method, path, json body).
CLI_ROUTES: list[tuple[str, str, Any]] = [
    ("GET", BASE + "/users", None),
    ("GET", BASE + "/users/alice", None),
    ("GET", BASE + "/groups", None),
    ("GET", BASE + "/sessions", None),
    ("GET", BASE + "/sessions/current", None),
    ("DELETE", BASE + "/sessions/current", None),
    ("POST", BASE + "/sessions/ws-ticket", None),
    ("GET", BASE + "/tokens", None),
    ("POST", BASE + "/tokens", {"name": "more"}),
    ("DELETE", BASE + "/tokens/some-id", None),
    ("GET", BASE + "/permissions", None),
    ("GET", BASE + "/audit", None),
    ("GET", BASE + "/link/code/ABCD-EFGH", None),
    ("PUT", BASE + "/users/alice/password", {"password": "newpass123", "currentPassword": "pass1234"}),
    ("GET", BASE + "/commands", None),
    ("POST", "/api/qcli/execute", {"command": "echo", "value": "hi"}),
    ("POST", BASE + "/execute", {"command": "echo", "value": "hi"}),
    ("GET", "/api/qcli/fs/ls?path=/", None),
    ("GET", BASE + "/jobs", None),
    ("POST", "/api/qcli/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
]


def _h(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class Srv:
    def __init__(self, client: TestClient, users: Any, result: Any) -> None:
        self.c = client
        self.users = users
        self.result = result

    def call(self, coro_fn, *args):
        return self.c.portal.call(coro_fn, *args)

    def login(self, name: str, password: str) -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": password})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    def alice(self, perms: list[str] | None = None) -> User:
        async def make() -> User:
            await self.users.ensure_seeded()
            await self.users.group_store.create_group("alice-g", permissions=perms or ["rates:read", "files:read"])
            user = await self.users.user_store.create_user("alice", "alice@example.com", ["users", "alice-g"])
            await self.users.auth_service.set_password(user.id, "pass1234")
            return user

        return self.call(make)

    def token(self, user: User, *, audience: str = "host", permissions: list[str] | None = None, name: str = "t") -> dict:
        perms = permissions if permissions is not None else (["rates:read"] if audience == "host" else None)
        created = self.call(
            self.users.api_tokens.create, user.id,
            ApiTokenCreateOptions(name=name, audience=audience, permissions=perms),
        )
        return created.to_dict()

    def request(self, method: str, path: str, token: str | None, body: Any = None):
        return self.c.request(method, path, headers=_h(token), json=body)


def _host_routes(result: Any, users: Any) -> None:
    @result.app.get("/api/host/rates", dependencies=[Depends(users.require_permission("rates:read"))])
    async def rates():
        return {"rates": [1.0]}

    @result.app.get("/host/me")
    async def me(ctx=Depends(users.require_auth)):
        return {"user": ctx.user.name, "audience": ctx.api_token.audience if ctx.api_token else None}


def _make(builder: CliUsersBuilder | None = None, **options: Any) -> tuple[Any, Any]:
    def configure(b):
        b.add_processor(CliEchoCommandProcessor())
        b.add_permissions([
            CliPermission(name="rates:read", description="Official exchange rates", group="Rates"),
            CliPermission(name="banks:read", description="Banks and their buy and sell rates", group="Rates"),
        ])

    result = create_cli_server(CliServerOptions(configure=configure, mcp=True, **options))
    users = (builder or CliUsersBuilder()).set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    result.mount_plugin(users)
    _host_routes(result, users)
    return result, users


@pytest.fixture()
def srv():
    result, users = _make()
    with TestClient(result.app) as client:
        yield Srv(client, users, result)


def test_is_cli_path():
    for path in ["/api/qcli/execute", "/api/v1/qcli/users", "/api/v2/cli/x", "/ws/qcli/shell",
                 "/ws/v1/qcli/events", "/api/qcli", "/custom/execute"]:
        assert is_cli_path(path, ["/custom"]), path
    for path in ["/api/host/rates", "/api/qclix", "/host/me", "/api/v1/rates", "/customer"]:
        assert not is_cli_path(path, ["/custom"]), path


class TestHostRoutes:
    def test_host_token_accepted_by_the_guards(self, srv: Srv):
        token = srv.token(srv.alice())["token"]
        res = srv.request("GET", "/api/host/rates", token)
        assert res.status_code == 200, res.text
        res = srv.request("GET", "/host/me", token)
        assert res.json() == {"user": "alice", "audience": "host"}

    def test_cli_token_still_accepted_by_the_guards(self, srv: Srv):
        token = srv.token(srv.alice(), audience="cli")["token"]
        assert srv.request("GET", "/api/host/rates", token).status_code == 200

    def test_guard_without_the_permission(self, srv: Srv):
        alice = srv.alice()
        token = srv.token(alice)["token"]
        srv.call(lambda: srv.users.group_store.update_group("alice-g", permissions=["files:read"]))
        res = srv.request("GET", "/api/host/rates", token)
        assert res.status_code == 403
        assert res.json() == {"detail": "missing permission: rates:read"}

    def test_host_token_guard_does_not_consult_the_catalog(self, srv: Srv, monkeypatch):
        token = srv.token(srv.alice())["token"]
        catalog = srv.users.auth_service.permission_catalog
        calls: list[str] = []
        monkeypatch.setattr(catalog, "find", lambda *a: calls.append("find"))
        monkeypatch.setattr(catalog, "all", lambda *a: calls.append("all") or [])
        assert srv.request("GET", "/api/host/rates", token).status_code == 200
        assert srv.request("GET", "/host/me", token).status_code == 200
        assert calls == []

    def test_revoked_host_token(self, srv: Srv):
        alice = srv.alice()
        created = srv.token(alice)
        assert srv.call(srv.users.api_tokens.revoke, alice.id, created["apiToken"]["id"]) is True
        assert srv.request("GET", "/api/host/rates", created["token"]).status_code == 401


class TestCliRoutes:
    @pytest.mark.parametrize("method,path,body", CLI_ROUTES)
    def test_refused(self, srv: Srv, method: str, path: str, body: Any):
        token = srv.token(srv.alice())["token"]
        res = srv.request(method, path, token, body)
        assert res.status_code == 403, res.text
        assert res.json() == {"error": HOST_TEXT}

    @pytest.mark.parametrize("method,path,body", CLI_ROUTES[:3] + CLI_ROUTES[14:16])
    def test_refused_with_empty_live_permissions(self, srv: Srv, method: str, path: str, body: Any):
        token = srv.token(srv.alice())["token"]
        srv.call(lambda: srv.users.group_store.update_group("alice-g", permissions=[]))
        res = srv.request(method, path, token, body)
        assert res.status_code == 403, res.text
        assert res.json() == {"error": HOST_TEXT}

    def test_refused_before_the_password_change_gate(self, srv: Srv):
        alice = srv.alice()
        token = srv.token(alice)["token"]
        srv.call(lambda: srv.users.user_store.update_user(alice.id, must_change_password=True))
        for method, path, body in (CLI_ROUTES[0], CLI_ROUTES[4], CLI_ROUTES[15]):
            res = srv.request(method, path, token, body)
            assert res.json() == {"error": HOST_TEXT}, path

    def test_cli_token_still_works(self, srv: Srv):
        token = srv.token(srv.alice(), audience="cli")["token"]
        assert srv.request("POST", "/api/qcli/execute", token, {"command": "echo", "value": "hi"}).status_code == 200
        assert srv.request("GET", BASE + "/permissions", token).status_code == 200

    def test_shell_socket_cannot_be_reached(self, srv: Srv):
        token = srv.token(srv.alice(["rates:read", "shell:execute"]))["token"]
        res = srv.request("POST", BASE + "/sessions/ws-ticket", token)
        assert res.status_code == 403 and res.json() == {"error": HOST_TEXT}

    def test_a_host_ticket_is_never_consumed(self, srv: Srv):
        token = srv.token(srv.alice())["token"]
        ctx = srv.call(srv.users.auth_service.authenticate_token, token)
        assert ctx.is_host_token
        assert srv.call(srv.users.auth_service.is_context_active, ctx) is False
        ticket, _ = srv.users.auth_service.issue_ticket(ctx)
        with srv.c.websocket_connect(f"/ws/qcli/events?ticket={ticket}") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 4401

    def test_shell_access_refuses_host_contexts(self, srv: Srv):
        from qodalis_cli_users.shell_access import UsersShellAccess, UsersShellPrincipal

        token = srv.token(srv.alice(["rates:read", "shell:execute"]))["token"]
        ctx = srv.call(srv.users.auth_service.authenticate_token, token)
        access = UsersShellAccess(srv.users.auth_service)
        principal = UsersShellPrincipal(user_id=ctx.user.id, user_name="alice", session_id=None, context=ctx)
        assert access.can_open(principal) is False


class TestVariants:
    def test_external_adapter_with_api_tokens(self):
        builder = CliUsersBuilder().set_auth_adapter(lambda req: None).set_allow_api_tokens()
        result, users = _make(builder)
        with TestClient(result.app) as client:
            srv = Srv(client, users, result)
            token = srv.token(srv.alice())["token"]
            assert srv.request("GET", "/api/host/rates", token).status_code == 200
            for method, path, body in (CLI_ROUTES[0], CLI_ROUTES[7], CLI_ROUTES[15]):
                res = srv.request(method, path, token, body)
                assert res.status_code == 403, path
                assert res.json() == {"error": HOST_TEXT}

    def test_server_not_protected(self):
        result, users = _make(CliUsersBuilder().set_protect_server(False))
        with TestClient(result.app) as client:
            srv = Srv(client, users, result)
            token = srv.token(srv.alice())["token"]
            # CLI routes are not authenticated: the header is ignored.
            assert srv.request("POST", "/api/qcli/execute", token, {"command": "echo", "value": "hi"}).status_code == 200
            # The users plugin's routes, and the audit API it guards, still refuse it.
            for path in (BASE + "/users", BASE + "/permissions", BASE + "/audit"):
                res = srv.request("GET", path, token)
                assert res.status_code == 403, path
                assert res.json() == {"error": HOST_TEXT}
            assert srv.request("GET", "/api/host/rates", token).status_code == 200
