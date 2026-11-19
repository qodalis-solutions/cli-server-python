"""Guarded host routes: the caller (``request.state.qcli_caller``), the
host-token-only guard (``require_host_token``) and the guard tags read by the
public OpenAPI document."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_users import CliUsersBuilder, QcliCaller

from tests.test_users_host_tokens import Srv, _make

HOST_TOKEN_REQUIRED = "forbidden: a host token is required"


def _caller_routes(result: Any, users: Any) -> None:
    @result.app.get("/host/caller")
    async def caller(request: Request, ctx=Depends(users.require_permission("rates:read"))):
        assert ctx.caller == request.state.qcli_caller
        return request.state.qcli_caller.to_dict()

    @result.app.get("/host/auth")
    async def auth(request: Request, ctx=Depends(users.require_auth)):
        return request.state.qcli_caller.to_dict()

    @result.app.get("/host/only")
    async def only(caller=Depends(users.require_host_token("rates:read"))):
        return {"tokenId": caller.caller.token_id}

    @result.app.get("/host/any-host")
    async def any_host(caller=Depends(users.require_host_token())):
        return {"ok": True}


@pytest.fixture()
def srv():
    result, users = _make()
    _caller_routes(result, users)
    with TestClient(result.app) as client:
        yield Srv(client, users, result)


def _get(srv: Srv, path: str, token: str | None):
    return srv.request("GET", path, token)


def test_caller_for_a_host_token(srv: Srv):
    alice = srv.alice()
    created = srv.token(alice)
    res = _get(srv, "/host/caller", created["token"])
    assert res.status_code == 200, res.text
    assert res.json() == {
        "userId": alice.id,
        "userName": "alice",
        "tokenId": created["apiToken"]["id"],
        "audience": "host",
        "permissions": ["rates:read"],
    }
    assert list(res.json()) == ["userId", "userName", "tokenId", "audience", "permissions"]


def test_caller_for_a_session(srv: Srv):
    alice = srv.alice()
    session = srv.login("alice", "pass1234")
    res = _get(srv, "/host/caller", session)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["userId"] == alice.id and body["userName"] == "alice"
    assert body["tokenId"] is None and body["audience"] is None
    assert "rates:read" in body["permissions"]


def test_caller_for_a_cli_token(srv: Srv):
    created = srv.token(srv.alice(), audience="cli", permissions=["rates:read"])
    res = _get(srv, "/host/caller", created["token"])
    assert res.json()["audience"] == "cli"
    assert res.json()["tokenId"] == created["apiToken"]["id"]


def test_require_auth_sets_the_caller(srv: Srv):
    created = srv.token(srv.alice())
    res = _get(srv, "/host/auth", created["token"])
    assert res.status_code == 200, res.text
    assert res.json()["audience"] == "host"


def test_caller_is_frozen():
    caller = QcliCaller(user_id="u", user_name="n", token_id=None, audience=None, permissions=("a",))
    with pytest.raises(Exception):
        caller.user_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize("kind", ["session", "cli"])
def test_host_token_only_refuses_session_and_cli_token(srv: Srv, kind: str):
    alice = srv.alice()
    token = srv.login("alice", "pass1234") if kind == "session" else srv.token(alice, audience="cli")["token"]
    res = _get(srv, "/host/only", token)
    assert res.status_code == 403
    assert res.json() == {"error": HOST_TOKEN_REQUIRED}


def test_host_token_only_accepts_a_host_token(srv: Srv):
    created = srv.token(srv.alice())
    res = _get(srv, "/host/only", created["token"])
    assert res.status_code == 200, res.text
    assert res.json() == {"tokenId": created["apiToken"]["id"]}
    assert _get(srv, "/host/any-host", created["token"]).status_code == 200


def test_host_token_only_checks_the_audience_first(srv: Srv):
    alice = srv.alice()
    cli = srv.token(alice, audience="cli", permissions=["files:read"], name="cli")["token"]
    res = _get(srv, "/host/only", cli)
    assert (res.status_code, res.json()) == (403, {"error": HOST_TOKEN_REQUIRED})

    host = srv.token(alice, name="host")["token"]
    srv.call(lambda: srv.users.group_store.update_group("alice-g", permissions=["files:read"]))
    res = _get(srv, "/host/only", host)
    assert (res.status_code, res.json()) == (403, {"error": "missing permission: rates:read"})


def test_host_token_only_authentication_texts(srv: Srv):
    res = _get(srv, "/host/only", None)
    assert (res.status_code, res.json()) == (401, {"error": "unauthorized"})
    res = _get(srv, "/host/only", "qcli_pat_unknown")
    assert (res.status_code, res.json()) == (401, {"error": "invalid or expired token"})


def test_host_token_only_password_change_required(srv: Srv):
    alice = srv.alice()
    host = srv.token(alice)["token"]

    async def must_change() -> None:
        await srv.users.user_store.update_user(alice.id, must_change_password=True)

    srv.call(must_change)
    res = _get(srv, "/host/only", host)
    assert (res.status_code, res.json()) == (403, {"error": "password change required"})


def test_require_permission_unchanged_for_cli_tokens_and_sessions(srv: Srv):
    alice = srv.alice()
    cli = srv.token(alice, audience="cli")["token"]
    session = srv.login("alice", "pass1234")
    assert _get(srv, "/host/caller", cli).status_code == 200
    assert _get(srv, "/host/caller", session).status_code == 200
    # The existing guards keep FastAPI's error shape on host routes.
    srv.call(lambda: srv.users.group_store.update_group("alice-g", permissions=["files:read"]))
    res = _get(srv, "/host/caller", cli)
    assert (res.status_code, res.json()) == (403, {"detail": "missing permission: rates:read"})


def test_require_permission_is_tagged():
    users = CliUsersBuilder().build()
    guard = users.require_permission("rates:read")
    assert guard.qcli_permission == "rates:read"
    assert guard.qcli_host_token_only is False
    host = users.require_host_token("rates:read")
    assert host.qcli_permission == "rates:read"
    assert host.qcli_host_token_only is True
    assert users.require_host_token().qcli_permission is None
    assert users.require_auth.qcli_permission is None
    assert users.require_auth.qcli_host_token_only is False


def test_app_docs_off():
    result = create_cli_server(CliServerOptions(app_docs=False))
    with TestClient(result.app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


def test_app_docs_default_on():
    result = create_cli_server()
    with TestClient(result.app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 200, path
