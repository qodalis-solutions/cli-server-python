"""API tokens (users plugin): ``/api/v1/qcli/tokens`` and ``qcli_pat_``
bearer authentication."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliEchoCommandProcessor, CliServerOptions, create_cli_server
from qodalis_cli.abstractions import CliCommandProcessor
from qodalis_cli_users import CliUsersBuilder, InMemoryApiTokenStore
from qodalis_cli_users.models import now_ms
from qodalis_cli_users.services.auth_service import hash_token

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
DAY_MS = 24 * 60 * 60 * 1000
TOKEN_RE = re.compile(r"^qcli_pat_[A-Za-z0-9_-]{43}$")


class DeployProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "deploy"

    @property
    def description(self) -> str:
        return "Deploys"

    @property
    def required_permissions(self) -> list[str]:
        return ["deploy:run"]

    async def handle_async(self, command, cancellation_event=None) -> str:
        return "deployed"


def _h(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def assert_error(res, status: int, contains: str | None = None) -> None:
    assert res.status_code == status, res.text
    body = res.json()
    assert set(body) == {"error"}, body
    if contains:
        assert contains in body["error"], body


class Srv:
    def __init__(self, client: TestClient, users, result) -> None:
        self.c = client
        self.users = users
        self.result = result

    def login(self, name: str, password: str) -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": password})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    @property
    def root(self) -> str:
        return self.login("root", ROOT_PASS)

    def user(self, name: str, perms: list[str] | None = None) -> str:
        admin = self.root
        groups = ["users"]
        if perms is not None:
            res = self.c.post(BASE + "/groups", headers=_h(admin), json={"name": f"{name}-g", "permissions": perms})
            assert res.status_code == 201, res.text
            groups.append(f"{name}-g")
        res = self.c.post(BASE + "/users", headers=_h(admin), json={
            "name": name, "email": f"{name}@example.com", "password": "pass1234", "groups": groups,
        })
        assert res.status_code == 201, res.text
        return self.login(name, "pass1234")

    def create(self, session: str, name: str = "ci", **body: Any) -> dict[str, Any]:
        res = self.c.post(BASE + "/tokens", headers=_h(session), json={"name": name, **body})
        assert res.status_code == 201, res.text
        return res.json()

    def execute(self, token: str, command: str = "echo", value: str = "hi"):
        return self.c.post("/api/qcli/execute", headers=_h(token), json={"command": command, "value": value})


def _make(builder: CliUsersBuilder | None = None) -> tuple[Any, Any]:
    def configure(b):
        b.add_processor(CliEchoCommandProcessor())
        b.add_processor(DeployProcessor())

    result = create_cli_server(CliServerOptions(configure=configure, allowed_origins=["http://allowed.test"]))
    users = (builder or CliUsersBuilder()).set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    result.mount_plugin(users)
    return result, users


@pytest.fixture()
def srv():
    result, users = _make()
    with TestClient(result.app) as client:
        yield Srv(client, users, result)


# =========================================================================
# Create / list / revoke
# =========================================================================


class TestCreate:
    def test_create_returns_token_once_and_view(self, srv: Srv):
        root = srv.root
        before = now_ms()
        body = srv.create(root, "my ci-token_1.0")
        assert set(body) == {"token", "apiToken"}
        assert TOKEN_RE.match(body["token"])
        view = body["apiToken"]
        assert set(view) == {"id", "name", "userId", "userName", "createdAt", "expiresAt", "lastUsedAt"}
        assert view["name"] == "my ci-token_1.0"
        assert view["userName"] == "root"
        assert view["lastUsedAt"] is None
        # Default expiry: 90 days.
        assert before + 90 * DAY_MS <= view["expiresAt"] <= now_ms() + 90 * DAY_MS
        # The store keeps only the hash.
        stored = srv.c.portal.call(srv.users.api_token_store.get_token, view["id"])
        assert stored.token_hash == hash_token(body["token"])
        assert body["token"] not in json.dumps(srv.c.get(BASE + "/tokens", headers=_h(root)).json())

    def test_expires_in_days(self, srv: Srv):
        view = srv.create(srv.root, "a", expiresInDays=7)["apiToken"]
        assert abs(view["expiresAt"] - (view["createdAt"] + 7 * DAY_MS)) < 1000

    @pytest.mark.parametrize("days", [0, 366, -1, 1.5, "7", True])
    def test_invalid_expires_in_days(self, srv: Srv, days):
        res = srv.c.post(BASE + "/tokens", headers=_h(srv.root), json={"name": "a", "expiresInDays": days})
        assert_error(res, 400, "expiresInDays")

    def test_never_expiring_needs_option(self, srv: Srv):
        res = srv.c.post(BASE + "/tokens", headers=_h(srv.root), json={"name": "a", "expiresInDays": None})
        assert_error(res, 400)

    def test_never_expiring_when_allowed(self):
        result, users = _make(CliUsersBuilder().set_allow_non_expiring_tokens())
        with TestClient(result.app) as client:
            s = Srv(client, users, result)
            view = s.create(s.root, "forever", expiresInDays=None)["apiToken"]
            assert view["expiresAt"] is None

    @pytest.mark.parametrize("name", ["", "   ", "a" * 65, "bad/name", "semi;colon", "émoji", 5, None])
    def test_invalid_names(self, srv: Srv, name):
        res = srv.c.post(BASE + "/tokens", headers=_h(srv.root), json={"name": name})
        assert_error(res, 400)

    def test_duplicate_name_case_insensitive_409(self, srv: Srv):
        root = srv.root
        srv.create(root, "Deploy")
        res = srv.c.post(BASE + "/tokens", headers=_h(root), json={"name": "deploy"})
        assert_error(res, 409)
        # Other users may reuse the name.
        srv.create(srv.user("alice"), "deploy")

    def test_max_20_tokens(self, srv: Srv):
        root = srv.root
        for i in range(20):
            srv.create(root, f"t{i}")
        res = srv.c.post(BASE + "/tokens", headers=_h(root), json={"name": "t20"})
        assert_error(res, 400, "20")

    def test_anonymous_401(self, srv: Srv):
        assert_error(srv.c.post(BASE + "/tokens", json={"name": "a"}), 401)
        assert_error(srv.c.get(BASE + "/tokens"), 401)

    def test_api_token_cannot_create_tokens(self, srv: Srv):
        token = srv.create(srv.root)["token"]
        res = srv.c.post(BASE + "/tokens", headers=_h(token), json={"name": "more"})
        assert_error(res, 403, "API tokens cannot create tokens")

    def test_audited(self, srv: Srv):
        root = srv.root
        view = srv.create(root, "audited")["apiToken"]
        assert srv.c.delete(f"{BASE}/tokens/{view['id']}", headers=_h(root)).status_code == 204
        entries = srv.c.get(BASE + "/audit", headers=_h(root)).json()
        events = {e["event"]: e for e in entries}
        for event in ("token_created", "token_revoked"):
            assert event in events
            assert "audited" in events[event]["details"] and view["id"] in events[event]["details"]
            assert events[event]["actor"] == "root"


class TestScope:
    def test_scope_must_be_held(self, srv: Srv):
        alice = srv.user("alice", ["logs:read"])
        res = srv.c.post(BASE + "/tokens", headers=_h(alice), json={"name": "a", "permissions": ["users:read"]})
        assert_error(res, 403, "cannot grant users:read")
        view = srv.create(alice, "ok", permissions=["logs:read", "commands:execute"])["apiToken"]
        assert view["permissions"] == ["logs:read", "commands:execute"]

    def test_invalid_permission_string(self, srv: Srv):
        res = srv.c.post(BASE + "/tokens", headers=_h(srv.root), json={"name": "a", "permissions": ["bad perm!"]})
        assert_error(res, 400)

    def test_scope_limits_permissions(self, srv: Srv):
        root = srv.root
        scoped = srv.create(root, "scoped", permissions=["commands:execute"])["token"]
        assert srv.execute(scoped).status_code == 200
        # deploy:run is not in the scope although root holds '*'.
        assert_error(srv.execute(scoped, "deploy"), 403, "deploy:run")
        assert_error(srv.c.get(BASE + "/users", headers=_h(scoped)), 403)
        full = srv.create(root, "full")["token"]
        assert srv.execute(full, "deploy").status_code == 200

    def test_scope_intersects_live_permissions(self, srv: Srv):
        alice = srv.user("alice", ["deploy:run"])
        token = srv.create(alice, "t", permissions=["deploy:run", "commands:execute"])["token"]
        assert srv.execute(token, "deploy").status_code == 200
        # Removing the permission from the user removes it from the token.
        res = srv.c.put(BASE + "/groups/alice-g", headers=_h(srv.root), json={"permissions": []})
        assert res.status_code == 200, res.text
        assert_error(srv.execute(token, "deploy"), 403)
        assert srv.execute(token).status_code == 200


class TestListAndRevoke:
    def test_list_own_and_all(self, srv: Srv):
        root = srv.root
        alice = srv.user("alice")
        srv.create(root, "root-t")
        srv.create(alice, "alice-t")
        own = srv.c.get(BASE + "/tokens", headers=_h(alice)).json()
        assert [t["name"] for t in own] == ["alice-t"]
        assert_error(srv.c.get(BASE + "/tokens?all=true", headers=_h(alice)), 403, "tokens:read")
        everything = srv.c.get(BASE + "/tokens?all=true", headers=_h(root)).json()
        assert {(t["name"], t["userName"]) for t in everything} == {("root-t", "root"), ("alice-t", "alice")}

    def test_tokens_read_permission(self, srv: Srv):
        bob = srv.user("bob", ["tokens:read"])
        srv.create(srv.root, "root-t")
        assert len(srv.c.get(BASE + "/tokens?all=true", headers=_h(bob)).json()) == 1

    def test_last_used_updated(self, srv: Srv):
        body = srv.create(srv.root)
        assert srv.execute(body["token"]).status_code == 200
        view = srv.c.get(BASE + "/tokens", headers=_h(srv.root)).json()[0]
        assert isinstance(view["lastUsedAt"], int)

    def test_owner_revokes(self, srv: Srv):
        alice = srv.user("alice")
        body = srv.create(alice)
        assert srv.execute(body["token"]).status_code == 200
        assert srv.c.delete(f"{BASE}/tokens/{body['apiToken']['id']}", headers=_h(alice)).status_code == 204
        assert_error(srv.execute(body["token"]), 401)
        assert srv.c.get(BASE + "/tokens", headers=_h(alice)).json() == []

    def test_other_user_404_without_tokens_revoke(self, srv: Srv):
        alice = srv.user("alice")
        bob = srv.user("bob")
        tid = srv.create(alice)["apiToken"]["id"]
        assert_error(srv.c.delete(f"{BASE}/tokens/{tid}", headers=_h(bob)), 404)
        assert_error(srv.c.delete(f"{BASE}/tokens/unknown", headers=_h(bob)), 404)
        carol = srv.user("carol", ["tokens:revoke"])
        assert srv.c.delete(f"{BASE}/tokens/{tid}", headers=_h(carol)).status_code == 204

    def test_admin_revokes_any(self, srv: Srv):
        alice = srv.user("alice")
        tid = srv.create(alice)["apiToken"]["id"]
        assert srv.c.delete(f"{BASE}/tokens/{tid}", headers=_h(srv.root)).status_code == 204


# =========================================================================
# Authentication with a token
# =========================================================================


class TestTokenAuthentication:
    def test_token_authenticates_everywhere(self, srv: Srv):
        token = srv.create(srv.root)["token"]
        assert srv.execute(token).json()["outputs"][0]["value"] == "hi"
        assert srv.c.get("/api/qcli/commands", headers=_h(token)).status_code == 200
        assert srv.c.get(BASE + "/users", headers=_h(token)).status_code == 200

    def test_unknown_token_401(self, srv: Srv):
        assert_error(srv.execute("qcli_pat_" + "A" * 43), 401)

    def test_expired_token_401(self, srv: Srv):
        body = srv.create(srv.root, expiresInDays=1)
        store = srv.users.api_token_store
        store._tokens[body["apiToken"]["id"]].expires_at = now_ms() - 1
        assert_error(srv.execute(body["token"]), 401)

    def test_disabled_user_token_fails_without_deletion(self, srv: Srv):
        alice = srv.user("alice")
        body = srv.create(alice)
        res = srv.c.put(BASE + "/users/alice", headers=_h(srv.root), json={"disabled": True})
        assert res.status_code == 200
        assert_error(srv.execute(body["token"]), 401)
        assert len(srv.c.get(BASE + "/tokens?all=true", headers=_h(srv.root)).json()) == 1
        srv.c.put(BASE + "/users/alice", headers=_h(srv.root), json={"disabled": False})
        assert srv.execute(body["token"]).status_code == 200

    def test_deleting_user_deletes_tokens(self, srv: Srv):
        alice = srv.user("alice")
        body = srv.create(alice)
        assert srv.c.delete(BASE + "/users/alice", headers=_h(srv.root)).status_code == 204
        assert srv.c.get(BASE + "/tokens?all=true", headers=_h(srv.root)).json() == []
        assert_error(srv.execute(body["token"]), 401)

    def test_sessions_current_is_404(self, srv: Srv):
        token = srv.create(srv.root)["token"]
        assert_error(srv.c.get(BASE + "/sessions/current", headers=_h(token)), 404, "not a session")
        assert_error(srv.c.delete(BASE + "/sessions/current", headers=_h(token)), 404, "not a session")
        # The token keeps working.
        assert srv.execute(token).status_code == 200

    def test_token_cannot_change_password(self, srv: Srv):
        alice = srv.user("alice")
        token = srv.create(alice)["token"]
        res = srv.c.put(BASE + "/users/alice/password", headers=_h(token),
                        json={"password": "newpass123", "currentPassword": "pass1234"})
        assert_error(res, 403, "API tokens cannot change passwords")
        root_token = srv.create(srv.root)["token"]
        res = srv.c.put(BASE + "/users/alice/password", headers=_h(root_token), json={"password": "newpass123"})
        assert_error(res, 403, "API tokens cannot change passwords")

    def test_must_change_password_applies(self, srv: Srv):
        alice = srv.user("alice")
        token = srv.create(alice)["token"]
        alice_id = srv.c.get(BASE + "/users/alice", headers=_h(srv.root)).json()["id"]
        srv.c.portal.call(lambda: srv.users.user_store.update_user(alice_id, must_change_password=True))
        assert_error(srv.execute(token), 403, "password change required")

    def test_custom_store(self):
        store = InMemoryApiTokenStore()
        result, users = _make(CliUsersBuilder().set_api_token_store(store))
        with TestClient(result.app) as client:
            s = Srv(client, users, result)
            s.create(s.root)
            assert len(client.portal.call(store.get_tokens)) == 1
            assert users.api_token_store is store


# =========================================================================
# Sockets opened with a token
# =========================================================================


def _closed(ws) -> WebSocketDisconnect:
    while True:
        try:
            ws.receive_text()
        except WebSocketDisconnect as exc:
            return exc


class TestTokenSockets:
    def _open(self, srv: Srv, token: str):
        res = srv.c.post(BASE + "/sessions/ws-ticket", headers=_h(token))
        assert res.status_code == 201, res.text
        cm = srv.c.websocket_connect(f"/ws/qcli/events?ticket={res.json()['ticket']}")
        ws = cm.__enter__()
        assert json.loads(ws.receive_text())["type"] == "connected"
        return cm, ws

    def test_revoke_closes_socket(self, srv: Srv):
        root = srv.root
        body = srv.create(root)
        cm, ws = self._open(srv, body["token"])
        try:
            assert srv.c.delete(f"{BASE}/tokens/{body['apiToken']['id']}", headers=_h(root)).status_code == 204
            exc = _closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")
        finally:
            cm.__exit__(None, None, None)

    def test_disable_user_closes_socket(self, srv: Srv):
        alice = srv.user("alice")
        body = srv.create(alice)
        cm, ws = self._open(srv, body["token"])
        try:
            srv.c.put(BASE + "/users/alice", headers=_h(srv.root), json={"disabled": True})
            exc = _closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")
        finally:
            cm.__exit__(None, None, None)

    def test_ticket_of_revoked_token_rejected(self, srv: Srv):
        root = srv.root
        body = srv.create(root)
        ticket = srv.c.post(BASE + "/sessions/ws-ticket", headers=_h(body["token"])).json()["ticket"]
        srv.c.delete(f"{BASE}/tokens/{body['apiToken']['id']}", headers=_h(root))
        with srv.c.websocket_connect(f"/ws/qcli/events?ticket={ticket}") as ws:
            assert _closed(ws).code == 4401
