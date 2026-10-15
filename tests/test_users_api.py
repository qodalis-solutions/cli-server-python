"""Integration tests for the users plugin API (server contract, Appendix A)."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from qodalis_cli_users import (
    CliUsersBuilder,
    CliUsersPlugin,
    ExternalAuthResult,
    User,
)

BASE = "/api/v1/qcli"
ROOT_PASS = "admin123"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_error(res, status: int, contains: str | None = None) -> None:
    assert res.status_code == status, res.text
    body = res.json()
    assert set(body.keys()) == {"error"}, body
    assert isinstance(body["error"], str) and body["error"]
    if contains is not None:
        assert contains in body["error"], body["error"]


class Api:
    """Thin helper around a TestClient bound to a users plugin."""

    def __init__(self, plugin: CliUsersPlugin, client: TestClient, events: list[dict]) -> None:
        self.plugin = plugin
        self.client = client
        self.events = events

    # raw verbs -------------------------------------------------------
    def get(self, path: str, token: str | None = None, **kw: Any):
        return self.client.get(BASE + path, headers=_auth(token) if token else {}, follow_redirects=False, **kw)

    def post(self, path: str, token: str | None = None, json: Any = None, **kw: Any):
        return self.client.post(BASE + path, headers=_auth(token) if token else {}, json=json, follow_redirects=False, **kw)

    def put(self, path: str, token: str | None = None, json: Any = None, **kw: Any):
        return self.client.put(BASE + path, headers=_auth(token) if token else {}, json=json, follow_redirects=False, **kw)

    def delete(self, path: str, token: str | None = None, **kw: Any):
        return self.client.delete(BASE + path, headers=_auth(token) if token else {}, follow_redirects=False, **kw)

    # helpers ---------------------------------------------------------
    def login_res(self, username: str, password: str):
        return self.post("/sessions", json={"username": username, "password": password})

    def login(self, username: str, password: str) -> str:
        res = self.login_res(username, password)
        assert res.status_code == 201, res.text
        return res.json()["token"]

    def login_full(self, username: str, password: str) -> dict:
        res = self.login_res(username, password)
        assert res.status_code == 201, res.text
        return res.json()

    @property
    def root(self) -> str:
        return self.login("root", ROOT_PASS)

    def create_user(self, token: str, name: str, password: str | None = "pass1234", **extra: Any) -> dict:
        body = {"name": name, "email": f"{name}@example.com", **extra}
        if password is not None:
            body["password"] = password
        res = self.post("/users", token, json=body)
        assert res.status_code == 201, res.text
        return res.json()

    def create_group(self, token: str, name: str, permissions: list[str] | None = None, **extra: Any) -> dict:
        body = {"name": name, "permissions": permissions or [], **extra}
        res = self.post("/groups", token, json=body)
        assert res.status_code == 201, res.text
        return res.json()

    def user_with_perms(self, admin: str, name: str, perms: list[str], password: str = "pass1234") -> str:
        """Create a non-admin user in a custom group with ``perms``; return its token."""
        group = f"{name}-group"
        self.create_group(admin, group, perms)
        # Also in the built-in ``users`` group (commands:execute), like any
        # regular account.
        self.create_user(admin, name, password, groups=[group, "users"])
        return self.login(name, password)

    def kicked(self) -> list[dict]:
        return [e["data"] for e in self.events if e["type"] == "session-kicked"]


def _make_api(**builder_opts: Any) -> Api:
    events: list[dict] = []
    builder = CliUsersBuilder().set_default_admin_password(ROOT_PASS)
    for name, value in builder_opts.items():
        getattr(builder, f"set_{name}")(value)
    plugin = builder.build(broadcast_fn=lambda msg: events.append(json.loads(msg)))
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    return Api(plugin, TestClient(app), events)


@pytest.fixture()
def api() -> Api:
    return _make_api()


@pytest.fixture()
def admin(api: Api) -> str:
    return api.root


# =========================================================================
# Login / logout
# =========================================================================


class TestLogin:
    def test_login_response_shape(self, api: Api):
        body = api.login_full("root", ROOT_PASS)
        assert set(body.keys()) == {"token", "session"}
        session = body["session"]
        assert set(session.keys()) == {"id", "user", "permissions", "loginTime", "lastActivity", "expiresAt"}
        assert session["permissions"] == ["*"]
        assert session["user"]["name"] == "root"
        assert session["user"]["email"] == "root@localhost"
        assert session["user"]["groups"] == ["admin"]
        assert session["user"]["disabled"] is False
        assert session["expiresAt"] == session["lastActivity"] + 30 * 60 * 1000

    def test_login_by_email(self, api: Api):
        assert api.login_res("root@localhost", ROOT_PASS).status_code == 201

    def test_login_name_case_insensitive(self, api: Api):
        assert api.login_res("ROOT", ROOT_PASS).status_code == 201

    def test_invalid_password(self, api: Api):
        assert_error(api.login_res("root", "wrong"), 401)

    def test_unknown_user(self, api: Api):
        assert_error(api.login_res("nobody", "pass"), 401)

    def test_missing_username_is_400(self, api: Api):
        assert_error(api.post("/sessions", json={"password": ROOT_PASS}), 400, "username")

    def test_missing_password_is_400(self, api: Api):
        assert_error(api.post("/sessions", json={"username": "root"}), 400, "password")

    def test_malformed_body_is_400(self, api: Api):
        res = api.client.post(BASE + "/sessions", content=b"{not json", headers={"content-type": "application/json"})
        assert_error(res, 400)

    def test_disabled_user_is_403(self, api: Api, admin: str):
        api.create_user(admin, "dis", disabled=True)
        assert_error(api.login_res("dis", "pass1234"), 403, "account disabled")

    def test_last_login_at_persisted(self, api: Api, admin: str):
        user = api.get("/users/root", admin).json()
        assert isinstance(user["lastLoginAt"], int)

    def test_login_audited(self, api: Api, admin: str):
        api.login_res("root", "bad")
        events = [e["event"] for e in api.get("/audit", admin).json()]
        assert "login" in events and "login_failed" in events


class TestLockout:
    def test_locks_after_five_failures(self, api: Api, admin: str):
        api.create_user(admin, "lucy")
        for _ in range(5):
            assert_error(api.login_res("lucy", "wrong"), 401)
        assert_error(api.login_res("lucy", "pass1234"), 429, "account locked, try again later")

    def test_failures_by_name_and_email_count_together(self, api: Api, admin: str):
        api.create_user(admin, "lucy")
        for i in range(5):
            api.login_res("lucy" if i % 2 else "lucy@example.com", "wrong")
        assert_error(api.login_res("lucy", "pass1234"), 429)

    def test_success_resets_counter(self, api: Api, admin: str):
        api.create_user(admin, "lucy")
        for _ in range(4):
            api.login_res("lucy", "wrong")
        assert api.login_res("lucy", "pass1234").status_code == 201
        for _ in range(4):
            api.login_res("lucy", "wrong")
        assert api.login_res("lucy", "pass1234").status_code == 201

    def test_lock_expires(self):
        api = _make_api(lockout=100, max_login_attempts=2)
        for _ in range(2):
            api.login_res("root", "wrong")
        assert_error(api.login_res("root", ROOT_PASS), 429)
        time.sleep(0.15)
        assert api.login_res("root", ROOT_PASS).status_code == 201

    def test_verify_password_failures_count(self, api: Api, admin: str):
        user = api.create_user(admin, "lucy")
        for _ in range(5):
            res = api.post(f"/users/{user['id']}/verify-password", admin, json={"password": "bad"})
            assert res.json() == {"valid": False}
        assert_error(api.login_res("lucy", "pass1234"), 429)
        assert_error(api.post(f"/users/{user['id']}/verify-password", admin, json={"password": "pass1234"}), 429)


class TestLogout:
    def test_logout_invalidates_token(self, api: Api):
        token = api.root
        assert api.delete("/sessions/current", token).status_code == 204
        assert_error(api.get("/sessions/current", token), 401)

    def test_logout_without_token(self, api: Api):
        assert_error(api.delete("/sessions/current"), 401)

    def test_logout_audited(self, api: Api):
        token = api.root
        api.delete("/sessions/current", token)
        assert any(e["event"] == "logout" for e in api.get("/audit", api.root).json())

    def test_invalid_bearer(self, api: Api):
        assert_error(api.get("/sessions/current", "nope"), 401)


# =========================================================================
# Sessions
# =========================================================================


class TestSessions:
    def test_current_same_shape_as_login(self, api: Api):
        body = api.login_full("root", ROOT_PASS)
        res = api.get("/sessions/current", body["token"])
        assert res.status_code == 200
        current = res.json()
        assert set(current.keys()) == set(body["session"].keys())
        assert current["id"] == body["session"]["id"]
        assert "token" not in json.dumps(current)

    def test_listing_never_includes_token(self, api: Api, admin: str):
        other = api.root
        res = api.get("/sessions", admin)
        assert res.status_code == 200
        sessions = res.json()
        assert len(sessions) == 2
        raw = json.dumps(sessions)
        assert admin not in raw and other not in raw
        for s in sessions:
            assert "token" not in s
            assert {"id", "userId", "userName", "createdAt", "lastActivity", "expiresAt", "metadata"} <= set(s)
            assert s["userName"] == "root"
            assert s["expiresAt"] == s["lastActivity"] + 30 * 60 * 1000
        assert sum(1 for s in sessions if s.get("current")) == 1

    def test_listing_metadata(self, api: Api):
        res = api.client.post(
            BASE + "/sessions", json={"username": "root", "password": ROOT_PASS},
            headers={"user-agent": "pytest-agent"},
        )
        token = res.json()["token"]
        listing = api.get("/sessions", token).json()
        assert listing[0]["metadata"]["userAgent"] == "pytest-agent"

    def test_list_requires_sessions_read(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert_error(api.get("/sessions", token), 403)
        reader = api.user_with_perms(admin, "reader", ["sessions:read"])
        assert api.get("/sessions", reader).status_code == 200

    def test_revoke_own_session_without_permission(self, api: Api, admin: str):
        api.user_with_perms(admin, "bob", [])
        first = api.login_full("bob", "pass1234")
        second = api.login("bob", "pass1234")
        res = api.delete(f"/sessions/{first['session']['id']}", second)
        assert res.status_code == 204
        assert_error(api.get("/sessions/current", first["token"]), 401)
        assert api.get("/sessions/current", second).status_code == 200

    def test_cannot_revoke_others_session_without_permission(self, api: Api, admin: str):
        bob = api.user_with_perms(admin, "bob", [])
        root_session = api.get("/sessions/current", admin).json()["id"]
        assert_error(api.delete(f"/sessions/{root_session}", bob), 403)

    def test_admin_revoke_broadcasts_and_audits(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        alice = api.login_full("alice", "pass1234")
        res = api.delete(f"/sessions/{alice['session']['id']}", admin)
        assert res.status_code == 204
        assert_error(api.get("/sessions/current", alice["token"]), 401)
        kicked = api.kicked()
        assert kicked == [{
            "sessionId": alice["session"]["id"],
            "userId": alice["session"]["user"]["id"],
            "reason": "revoked",
        }]
        entries = api.get("/audit?event=session_revoked", admin).json()
        assert entries and entries[0]["actor"] == "root" and entries[0]["username"] == "alice"

    def test_revoke_unknown_session(self, api: Api, admin: str):
        assert_error(api.delete("/sessions/no-such-session", admin), 404)

    def test_idle_timeout_expires_session(self):
        api = _make_api(session_timeout=150)
        token = api.root
        assert api.get("/sessions/current", token).status_code == 200
        time.sleep(0.25)
        assert_error(api.get("/sessions/current", token), 401)
        # Expired session is deleted
        assert len(api.plugin.session_store._sessions) == 0  # type: ignore[attr-defined]

    def test_activity_slides_expiry(self):
        api = _make_api(session_timeout=300)
        token = api.root
        for _ in range(4):
            time.sleep(0.1)
            assert api.get("/sessions/current", token).status_code == 200
        first = api.get("/sessions/current", token).json()
        time.sleep(0.01)
        second = api.get("/sessions/current", token).json()
        assert second["lastActivity"] >= first["lastActivity"]
        assert second["expiresAt"] == second["lastActivity"] + 300


class TestLiveUserResolution:
    def test_group_change_applies_immediately(self, api: Api, admin: str):
        api.create_group(admin, "readers", ["users:read"])
        api.create_user(admin, "bob")
        token = api.login("bob", "pass1234")
        assert_error(api.get("/users", token), 403)
        assert api.post("/groups/readers/members/bob", admin).status_code == 204
        assert api.get("/users", token).status_code == 200
        current = api.get("/sessions/current", token).json()
        assert "readers" in current["user"]["groups"]
        assert current["permissions"] == ["commands:execute", "users:read"]

    def test_group_permission_change_applies_immediately(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", ["users:read"])
        assert api.get("/users", token).status_code == 200
        api.put("/groups/bob-group", admin, json={"permissions": []})
        assert_error(api.get("/users", token), 403)

    def test_disabled_user_gets_401(self, api: Api, admin: str):
        api.create_user(admin, "bob")
        token = api.login("bob", "pass1234")
        api.put("/users/bob", admin, json={"disabled": True})
        assert_error(api.get("/sessions/current", token), 401)

    def test_disabled_user_rejected_even_if_session_survives(self, api: Api, admin: str):
        api.create_user(admin, "bob")
        token = api.login("bob", "pass1234")
        user_id = api.get("/users/bob", admin).json()["id"]
        # Bypass the API (and its cascade) to disable the user in the store.
        import asyncio
        asyncio.run(api.plugin.user_store.update_user(user_id, disabled=True))
        assert_error(api.get("/sessions/current", token), 401)

    def test_deleted_user_gets_401(self, api: Api, admin: str):
        api.create_user(admin, "bob")
        token = api.login("bob", "pass1234")
        api.delete("/users/bob", admin)
        assert_error(api.get("/sessions/current", token), 401)

    def test_rename_reflected_in_current(self, api: Api, admin: str):
        api.create_user(admin, "bob")
        token = api.login("bob", "pass1234")
        api.put("/users/bob", admin, json={"name": "robert"})
        assert api.get("/sessions/current", token).json()["user"]["name"] == "robert"


# =========================================================================
# Users
# =========================================================================


class TestUsers:
    def test_list_requires_auth(self, api: Api):
        assert_error(api.get("/users"), 401)

    def test_list_requires_users_read(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert_error(api.get("/users", token), 403)

    def test_list_query_skip_take(self, api: Api, admin: str):
        for n in ("anna", "annabel", "bert"):
            api.create_user(admin, n)
        names = lambda res: [u["name"] for u in res.json()]  # noqa: E731
        assert names(api.get("/users?query=ANN", admin)) == ["anna", "annabel"]
        assert names(api.get("/users?query=bert@EXAMPLE", admin)) == ["bert"]
        assert names(api.get("/users?skip=1&take=2", admin)) == ["anna", "annabel"]
        assert names(api.get("/users?take=1", admin)) == ["root"]
        assert names(api.get("/users?query=ann&skip=1", admin)) == ["annabel"]
        assert_error(api.get("/users?take=abc", admin), 400)
        assert_error(api.get("/users?skip=-1", admin), 400)

    def test_create_user_shape_and_defaults(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        assert user["groups"] == ["users"]
        assert user["disabled"] is False
        assert "homeDir" not in user and "lastLoginAt" not in user and "data" not in user
        assert {"id", "name", "email", "createdAt", "updatedAt"} <= set(user)

    def test_create_user_all_fields(self, api: Api, admin: str):
        user = api.create_user(
            admin, "alice", groups=["ADMIN"], disabled=True, homeDir="/home/alice", data={"k": 1},
        )
        assert user["groups"] == ["admin"]  # canonical stored group name
        assert user["disabled"] is True
        assert user["homeDir"] == "/home/alice"
        assert user["data"] == {"k": 1}

    def test_create_requires_users_create(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", ["users:read"])
        assert_error(api.post("/users", token, json={"name": "x", "email": "x@x.io"}), 403)
        creator = api.user_with_perms(admin, "carl", ["users:create"])
        assert api.post("/users", creator, json={"name": "x", "email": "x@x.io"}).status_code == 201

    def test_create_validation(self, api: Api, admin: str):
        assert_error(api.post("/users", admin, json={"email": "a@b.io"}), 400, "name")
        assert_error(api.post("/users", admin, json={"name": "a"}), 400, "email")
        assert_error(api.post("/users", admin, json={"name": " ", "email": "a@b.io"}), 400)
        assert_error(api.post("/users", admin, json={"name": "a", "email": "a@b.io", "groups": "admin"}), 400)
        assert_error(api.post("/users", admin, json={"name": "a", "email": "a@b.io", "password": "abcdefg"}), 400, "at least 8")

    def test_create_unknown_group(self, api: Api, admin: str):
        res = api.post("/users", admin, json={"name": "a", "email": "a@b.io", "groups": ["nope"]})
        assert_error(res, 400, "unknown group: nope")

    def test_duplicate_name_and_email(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        assert_error(api.post("/users", admin, json={"name": "ALICE", "email": "z@z.io"}), 409)
        assert_error(api.post("/users", admin, json={"name": "other", "email": "Alice@Example.com"}), 409)

    def test_get_user_by_name_and_id(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        assert api.get("/users/alice", admin).json()["id"] == user["id"]
        assert api.get(f"/users/{user['id']}", admin).json()["name"] == "alice"
        assert_error(api.get("/users/no-such-user", admin), 404)

    def test_get_self_without_permission(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert api.get("/users/bob", token).status_code == 200
        assert_error(api.get("/users/root", token), 403)

    def test_user_permissions_endpoint(self, api: Api, admin: str):
        api.create_group(admin, "ops", ["deploy:run", "users:read"])
        api.create_group(admin, "dev", ["deploy:run", "logs:*"])
        user = api.create_user(admin, "alice", groups=["ops", "dev"])
        token = api.login("alice", "pass1234")
        res = api.get("/users/alice/permissions", token)
        assert res.status_code == 200
        assert res.json() == {
            "userId": user["id"],
            "groups": ["ops", "dev"],
            "permissions": ["deploy:run", "users:read", "logs:*"],
        }
        assert api.get("/users/root/permissions", token).json()["permissions"] == ["*"]
        nobody = api.user_with_perms(admin, "nobody", [])
        assert_error(api.get("/users/alice/permissions", nobody), 403)

    def test_update_user(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        res = api.put(f"/users/{user['id']}", admin, json={"email": "new@example.com", "homeDir": "/h"})
        assert res.status_code == 200
        assert res.json()["email"] == "new@example.com"
        assert res.json()["homeDir"] == "/h"

    def test_update_rename_and_duplicates(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        api.create_user(admin, "bob")
        assert_error(api.put("/users/alice", admin, json={"name": "BOB"}), 409)
        assert_error(api.put("/users/alice", admin, json={"email": "bob@example.com"}), 409)
        res = api.put("/users/alice", admin, json={"name": "alicia"})
        assert res.status_code == 200 and res.json()["name"] == "alicia"
        # renaming to the same name (other case) is fine
        assert api.put("/users/alicia", admin, json={"name": "Alicia"}).status_code == 200

    def test_update_unknown_group(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        assert_error(api.put("/users/alice", admin, json={"groups": ["nope"]}), 400, "unknown group: nope")

    def test_update_nonexistent(self, api: Api, admin: str):
        assert_error(api.put("/users/ghost", admin, json={"email": "g@g.io"}), 404)

    def test_self_service_update(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        res = api.put("/users/bob", token, json={"email": "bobby@example.com", "data": {"theme": "dark"}})
        assert res.status_code == 200
        assert res.json()["data"] == {"theme": "dark"}
        for body in ({"name": "x"}, {"groups": ["admin"]}, {"disabled": True}, {"homeDir": "/x"}):
            assert_error(api.put("/users/bob", token, json=body), 403)
        assert_error(api.put("/users/root", token, json={"email": "x@x.io"}), 403)

    def test_update_requires_users_update(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        api.create_group(admin, "editors", ["docs:read"])
        updater = api.user_with_perms(admin, "upd", ["users:update", "docs:*"])
        assert api.put("/users/alice", updater, json={"groups": ["users", "editors"]}).status_code == 200

    def test_delete_user_cascades(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        alice = api.login_full("alice", "pass1234")
        res = api.delete("/users/alice", admin)
        assert res.status_code == 204
        assert_error(api.get(f"/users/{user['id']}", admin), 404)
        import asyncio
        assert asyncio.run(api.plugin.credential_store.get_credentials(user["id"])) is None
        assert_error(api.get("/sessions/current", alice["token"]), 401)
        assert {"sessionId": alice["session"]["id"], "userId": user["id"], "reason": "user_deleted"} in api.kicked()
        assert api.get("/audit?event=user_deleted", admin).json()[0]["actor"] == "root"

    def test_delete_requires_users_delete(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        token = api.user_with_perms(admin, "bob", ["users:read", "users:update"])
        assert_error(api.delete("/users/alice", token), 403)

    def test_delete_nonexistent(self, api: Api, admin: str):
        assert_error(api.delete("/users/ghost", admin), 404)

    def test_cannot_delete_yourself(self, api: Api, admin: str):
        api.create_user(admin, "admin2", groups=["admin"])
        assert_error(api.delete("/users/root", admin), 409, "yourself")

    def test_disable_cascades(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        s1 = api.login_full("alice", "pass1234")
        s2 = api.login_full("alice", "pass1234")
        res = api.put("/users/alice", admin, json={"disabled": True})
        assert res.status_code == 200 and res.json()["disabled"] is True
        for s in (s1, s2):
            assert_error(api.get("/sessions/current", s["token"]), 401)
        reasons = {(k["sessionId"], k["reason"]) for k in api.kicked()}
        assert reasons == {(s1["session"]["id"], "user_disabled"), (s2["session"]["id"], "user_disabled")}
        assert all(k["userId"] == user["id"] for k in api.kicked())
        events = [e["event"] for e in api.get("/audit", admin).json()]
        assert "user_disabled" in events
        api.put("/users/alice", admin, json={"disabled": False})
        assert "user_enabled" in [e["event"] for e in api.get("/audit", admin).json()]
        assert api.login_res("alice", "pass1234").status_code == 201


class TestLastAdminProtection:
    @pytest.fixture()
    def ops(self, api: Api, admin: str) -> str:
        # Holds '*' (so it covers root) without being a member of 'admin'.
        return api.user_with_perms(admin, "ops", ["*"])

    def test_cannot_delete_last_admin(self, api: Api, ops: str):
        assert_error(api.delete("/users/root", ops), 409)

    def test_cannot_disable_last_admin(self, api: Api, ops: str, admin: str):
        assert_error(api.put("/users/root", ops, json={"disabled": True}), 409)
        assert_error(api.put("/users/root", admin, json={"disabled": True}), 409)

    def test_cannot_remove_admin_group_from_last_admin(self, api: Api, ops: str):
        assert_error(api.put("/users/root", ops, json={"groups": ["users"]}), 409)
        assert_error(api.delete("/groups/admin/members/root", ops), 409)

    def test_disabled_admins_do_not_count(self, api: Api, ops: str, admin: str):
        api.create_user(admin, "admin2", groups=["admin"], disabled=True)
        assert_error(api.delete("/users/root", ops), 409)

    def test_allowed_with_second_enabled_admin(self, api: Api, ops: str, admin: str):
        api.create_user(admin, "admin2", groups=["admin"])
        assert api.delete("/groups/admin/members/root", ops).status_code == 204
        assert api.post("/groups/admin/members/root", ops).status_code == 204
        assert api.delete("/users/root", ops).status_code == 204


class TestPasswords:
    def test_admin_changes_others_password_revokes_all(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        alice = api.login_full("alice", "pass1234")
        res = api.put(f"/users/{user['id']}/password", admin, json={"password": "newpass1"})
        assert res.status_code == 204
        assert_error(api.get("/sessions/current", alice["token"]), 401)
        assert {"sessionId": alice["session"]["id"], "userId": user["id"], "reason": "password_changed"} in api.kicked()
        assert_error(api.login_res("alice", "pass1234"), 401)
        assert api.login_res("alice", "newpass1").status_code == 201
        entry = api.get("/audit?event=password_changed", admin).json()[0]
        assert entry["username"] == "alice" and entry["actor"] == "root"

    def test_self_change_revokes_other_sessions_only(self, api: Api, admin: str):
        api.user_with_perms(admin, "bob", [])
        current = api.login_full("bob", "pass1234")
        other = api.login_full("bob", "pass1234")
        res = api.put("/users/bob/password", current["token"], json={"currentPassword": "pass1234", "password": "bobpass2"})
        assert res.status_code == 204
        assert api.get("/sessions/current", current["token"]).status_code == 200
        assert_error(api.get("/sessions/current", other["token"]), 401)
        kicked_ids = {k["sessionId"] for k in api.kicked()}
        assert other["session"]["id"] in kicked_ids
        assert current["session"]["id"] not in kicked_ids

    def test_self_change_requires_current_password(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert_error(api.put("/users/bob/password", token, json={"password": "newpass1"}), 400)
        assert_error(api.put("/users/bob/password", token, json={"currentPassword": "wrong", "password": "newpass1"}), 403)

    def test_admin_self_change_no_current_needed(self, api: Api, admin: str):
        assert api.put("/users/root/password", admin, json={"password": "rootpass2"}).status_code == 204

    def test_policy(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        assert_error(api.put("/users/alice/password", admin, json={"password": "abcdefg"}), 400, "at least 8")
        assert_error(api.put("/users/alice/password", admin, json={}), 400)

    def test_min_password_length_configurable(self):
        api = _make_api(min_password_length=8)
        assert_error(api.put("/users/root/password", api.root, json={"password": "short12"}), 400, "at least 8")

    def test_cannot_change_others_password_without_permission(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert_error(api.put("/users/root/password", token, json={"password": "hacked"}), 403)

    def test_legacy_new_password_field(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        assert api.put("/users/alice/password", admin, json={"newPassword": "legacy12"}).status_code == 204
        assert api.login_res("alice", "legacy12").status_code == 201

    def test_unknown_user(self, api: Api, admin: str):
        assert_error(api.put("/users/ghost/password", admin, json={"password": "whatever"}), 404)


class TestVerifyPassword:
    def test_self(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert api.post("/users/bob/verify-password", token, json={"password": "pass1234"}).json() == {"valid": True}
        assert api.post("/users/bob/verify-password", token, json={"password": "nope"}).json() == {"valid": False}

    def test_other_requires_users_update(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", ["users:read"])
        assert_error(api.post("/users/root/verify-password", token, json={"password": ROOT_PASS}), 403)
        assert api.post("/users/bob/verify-password", admin, json={"password": "pass1234"}).json() == {"valid": True}

    def test_failure_audited(self, api: Api, admin: str):
        api.post("/users/root/verify-password", admin, json={"password": "bad"})
        entry = api.get("/audit?event=login_failed", admin).json()[0]
        assert entry["username"] == "root"

    def test_missing_password(self, api: Api, admin: str):
        assert_error(api.post("/users/root/verify-password", admin, json={}), 400)

    def test_requires_auth(self, api: Api):
        assert_error(api.post("/users/root/verify-password", json={"password": ROOT_PASS}), 401)


# =========================================================================
# Groups
# =========================================================================


class TestGroups:
    def test_builtins_seeded(self, api: Api, admin: str):
        groups = {g["name"]: g for g in api.get("/groups", admin).json()}
        assert groups["admin"]["permissions"] == ["*"] and groups["admin"]["builtIn"] is True
        assert groups["users"]["permissions"] == ["commands:execute"] and groups["users"]["builtIn"] is True
        for g in groups.values():
            assert {"id", "name", "permissions", "builtIn", "createdAt", "updatedAt"} <= set(g)

    def test_any_authenticated_user_can_read_groups(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", [])
        assert api.get("/groups", token).status_code == 200
        assert api.get("/groups/admin", token).status_code == 200
        assert_error(api.get("/groups"), 401)

    def test_create_group(self, api: Api, admin: str):
        res = api.post("/groups", admin, json={"name": "Editors", "description": "Content", "permissions": ["docs:*"]})
        assert res.status_code == 201
        body = res.json()
        assert body["name"] == "Editors" and body["description"] == "Content"
        assert body["permissions"] == ["docs:*"] and body["builtIn"] is False
        assert api.get("/groups/editors", admin).json()["name"] == "Editors"  # case-insensitive lookup
        assert api.get("/audit?event=group_created", admin).json()[0]["actor"] == "root"

    def test_create_validation(self, api: Api, admin: str):
        assert_error(api.post("/groups", admin, json={}), 400)
        assert_error(api.post("/groups", admin, json={"name": ""}), 400)
        assert_error(api.post("/groups", admin, json={"name": "x", "permissions": "users:*"}), 400)

    def test_duplicate_group(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        assert_error(api.post("/groups", admin, json={"name": "EDITORS"}), 409)
        assert_error(api.post("/groups", admin, json={"name": "admin"}), 409)

    def test_create_requires_groups_create(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", ["groups:read", "groups:update"])
        assert_error(api.post("/groups", token, json={"name": "x"}), 403)

    def test_get_unknown_group(self, api: Api, admin: str):
        assert_error(api.get("/groups/nope", admin), 404)

    def test_update_group(self, api: Api, admin: str):
        api.create_group(admin, "editors", ["docs:read"])
        res = api.put("/groups/editors", admin, json={"description": "Eds", "permissions": ["docs:*", "users:read"]})
        assert res.status_code == 200
        assert res.json()["description"] == "Eds"
        assert res.json()["permissions"] == ["docs:*", "users:read"]
        assert api.get("/audit?event=group_updated", admin).json()

    def test_update_admin_permissions_conflict(self, api: Api, admin: str):
        assert_error(api.put("/groups/admin", admin, json={"permissions": ["users:read"]}), 409)
        assert api.put("/groups/admin", admin, json={"description": "Admins"}).status_code == 200

    def test_update_requires_groups_update(self, api: Api, admin: str):
        token = api.user_with_perms(admin, "bob", ["groups:read"])
        assert_error(api.put("/groups/users", token, json={"permissions": ["*"]}), 403)

    def test_update_unknown(self, api: Api, admin: str):
        assert_error(api.put("/groups/nope", admin, json={"description": "x"}), 404)

    def test_delete_builtin_conflict(self, api: Api, admin: str):
        assert_error(api.delete("/groups/admin", admin), 409)
        assert_error(api.delete("/groups/users", admin), 409)

    def test_delete_group_cascades(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        api.create_user(admin, "alice", groups=["editors", "users"])
        assert api.delete("/groups/EDITORS", admin).status_code == 204
        assert_error(api.get("/groups/editors", admin), 404)
        assert api.get("/users/alice", admin).json()["groups"] == ["users"]
        assert api.get("/audit?event=group_deleted", admin).json()

    def test_delete_requires_groups_delete(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        token = api.user_with_perms(admin, "bob", ["groups:update"])
        assert_error(api.delete("/groups/editors", token), 403)

    def test_delete_unknown(self, api: Api, admin: str):
        assert_error(api.delete("/groups/nope", admin), 404)

    def test_add_and_remove_member_by_username(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        api.create_user(admin, "alice")
        res = api.post("/groups/Editors/members/alice", admin)
        assert res.status_code == 204
        assert api.get("/users/alice", admin).json()["groups"] == ["users", "editors"]
        assert api.post("/groups/editors/members/alice", admin).status_code == 204  # idempotent
        assert api.get("/users/alice", admin).json()["groups"] == ["users", "editors"]
        res = api.delete("/groups/editors/members/alice", admin)
        assert res.status_code == 204
        assert api.get("/users/alice", admin).json()["groups"] == ["users"]
        events = [e["event"] for e in api.get("/audit", admin).json()]
        assert "group_member_added" in events and "group_member_removed" in events

    def test_add_member_by_id(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        user = api.create_user(admin, "alice")
        assert api.post(f"/groups/editors/members/{user['id']}", admin).status_code == 204

    def test_member_errors(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        api.create_user(admin, "alice")
        assert_error(api.post("/groups/nope/members/alice", admin), 400, "unknown group: nope")
        assert_error(api.post("/groups/editors/members/ghost", admin), 404)
        assert_error(api.delete("/groups/nope/members/alice", admin), 404)
        assert_error(api.delete("/groups/editors/members/ghost", admin), 404)

    def test_membership_requires_groups_update(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        token = api.user_with_perms(admin, "bob", ["groups:read"])
        assert_error(api.post("/groups/editors/members/bob", token), 403)
        assert_error(api.delete("/groups/bob-group/members/bob", token), 403)

    def test_list_members(self, api: Api, admin: str):
        api.create_group(admin, "editors")
        api.create_user(admin, "alice", groups=["editors"])
        api.create_user(admin, "carl")
        alice = api.login("alice", "pass1234")
        res = api.get("/groups/editors/members", alice)  # member
        assert res.status_code == 200
        assert [u["name"] for u in res.json()] == ["alice"]
        carl = api.login("carl", "pass1234")
        assert_error(api.get("/groups/editors/members", carl), 403)
        reader = api.user_with_perms(admin, "reader", ["groups:read"])
        assert api.get("/groups/editors/members", reader).status_code == 200
        assert_error(api.get("/groups/nope/members", admin), 404)


# =========================================================================
# Audit
# =========================================================================


class TestAudit:
    def test_entries_shape_and_filters(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        api.login_res("alice", "pass1234")
        entries = api.get("/audit", admin).json()
        assert entries
        for e in entries:
            assert {"id", "timestamp", "event", "username"} <= set(e)
        created = api.get("/audit?event=user_created", admin).json()
        assert created[0]["username"] == "alice" and created[0]["actor"] == "root"
        assert all(e["username"] == "alice" for e in api.get("/audit?username=alice", admin).json())
        assert len(api.get("/audit?limit=1", admin).json()) == 1
        # newest first
        stamps = [e["timestamp"] for e in entries]
        assert stamps == sorted(stamps, reverse=True)

    def test_permissions(self, api: Api, admin: str):
        assert_error(api.get("/audit"), 401)
        token = api.user_with_perms(admin, "auditor", ["audit:read"])
        assert api.get("/audit", token).status_code == 200
        assert_error(api.delete("/audit", token), 403)
        nobody = api.user_with_perms(admin, "nobody", [])
        assert_error(api.get("/audit", nobody), 403)

    def test_clear(self, api: Api, admin: str):
        res = api.delete("/audit", admin)
        assert res.status_code == 204
        entries = api.get("/audit", admin).json()
        assert [e["event"] for e in entries] == ["audit_cleared"]
        assert entries[0]["actor"] == "root"

    def test_bad_limit(self, api: Api, admin: str):
        assert_error(api.get("/audit?limit=x", admin), 400)


# =========================================================================
# Permission matrix (non-admin user in a custom group)
# =========================================================================


MATRIX = [
    # (method, path, body, required permission)
    ("GET", "/users", None, "users:read"),
    ("POST", "/users", {"name": "newbie", "email": "newbie@x.io"}, "users:create"),
    ("PUT", "/users/target", {"groups": ["users"]}, "users:update"),
    ("DELETE", "/users/target", None, "users:delete"),
    ("GET", "/users/target", None, "users:read"),
    ("GET", "/users/target/permissions", None, "users:read"),
    ("PUT", "/users/target/password", {"password": "changed1"}, "users:update"),
    ("POST", "/users/target/verify-password", {"password": "pass1234"}, "users:update"),
    ("GET", "/groups/target-group/members", None, "groups:read"),
    ("POST", "/groups", {"name": "fresh"}, "groups:create"),
    ("PUT", "/groups/target-group", {"description": "d"}, "groups:update"),
    ("DELETE", "/groups/target-group", None, "groups:delete"),
    ("POST", "/groups/target-group/members/target", None, "groups:update"),
    ("DELETE", "/groups/target-group/members/target", None, "groups:update"),
    ("GET", "/sessions", None, "sessions:read"),
    ("GET", "/audit", None, "audit:read"),
    ("DELETE", "/audit", None, "audit:clear"),
]


def _call(api: Api, method: str, path: str, token: str, body: Any):
    if method == "GET":
        return api.get(path, token)
    if method == "DELETE":
        return api.delete(path, token)
    if method == "POST":
        return api.post(path, token, json=body)
    return api.put(path, token, json=body)


class TestPermissionMatrix:
    @pytest.mark.parametrize("method,path,body,perm", MATRIX)
    def test_denied_without_permission(self, api: Api, admin: str, method, path, body, perm):
        api.create_group(admin, "target-group")
        api.create_user(admin, "target")
        # Has every permission of the same resource except the required one.
        resource = perm.split(":")[0]
        others = [p for p in (f"{resource}:read", f"{resource}:create", f"{resource}:update",
                              f"{resource}:delete", f"{resource}:revoke", f"{resource}:clear") if p != perm]
        token = api.user_with_perms(admin, "worker", others)
        assert_error(_call(api, method, path, token, body), 403)

    @pytest.mark.parametrize("method,path,body,perm", MATRIX)
    def test_allowed_with_exact_permission(self, api: Api, admin: str, method, path, body, perm):
        api.create_group(admin, "target-group")
        api.create_user(admin, "target")
        token = api.user_with_perms(admin, "worker", [perm])
        res = _call(api, method, path, token, body)
        assert res.status_code in (200, 201, 204), (perm, res.status_code, res.text)

    @pytest.mark.parametrize("method,path,body,perm", MATRIX)
    def test_allowed_with_wildcard(self, api: Api, admin: str, method, path, body, perm):
        api.create_group(admin, "target-group")
        api.create_user(admin, "target")
        token = api.user_with_perms(admin, "worker", [perm.split(":")[0].upper() + ":*"])
        res = _call(api, method, path, token, body)
        assert res.status_code in (200, 201, 204), (perm, res.status_code, res.text)

    def test_revoke_session_requires_sessions_revoke(self, api: Api, admin: str):
        api.create_user(admin, "alice")
        alice_session = api.login_full("alice", "pass1234")["session"]["id"]
        denied = api.user_with_perms(admin, "reader", ["sessions:read"])
        assert_error(api.delete(f"/sessions/{alice_session}", denied), 403)
        revoker = api.user_with_perms(admin, "revoker", ["sessions:revoke"])
        assert api.delete(f"/sessions/{alice_session}", revoker).status_code == 204


# =========================================================================
# Routing / error format
# =========================================================================


COLLECTIONS = ["/users", "/groups", "/sessions", "/audit"]


class TestRouting:
    @pytest.mark.parametrize("path", COLLECTIONS)
    def test_get_with_and_without_trailing_slash(self, api: Api, admin: str, path: str):
        for p in (path, path + "/"):
            res = api.get(p, admin)
            assert res.status_code == 200, (p, res.status_code)
            assert isinstance(res.json(), list)

    def test_post_with_and_without_trailing_slash(self, api: Api, admin: str):
        assert api.post("/users/", admin, json={"name": "a1", "email": "a1@x.io"}).status_code == 201
        assert api.post("/users", admin, json={"name": "a2", "email": "a2@x.io"}).status_code == 201
        assert api.post("/groups/", admin, json={"name": "g1"}).status_code == 201
        assert api.post("/groups", admin, json={"name": "g2"}).status_code == 201
        assert api.post("/sessions/", json={"username": "root", "password": ROOT_PASS}).status_code == 201
        assert api.delete("/audit/", admin).status_code == 204
        assert api.delete("/audit", admin).status_code == 204

    def test_validation_errors_only_mapped_for_users_routes(self):
        plugin = CliUsersBuilder().build()
        app = FastAPI()

        class Body(BaseModel):
            x: int

        @app.post("/other")
        async def other(body: Body):
            return body

        app.include_router(plugin.router, prefix=plugin.prefix)
        client = TestClient(app)
        assert client.post("/other", json={}).status_code == 422
        assert "detail" in client.post("/other", json={}).json()
        assert_error(client.post(BASE + "/sessions", json={}), 400)


# =========================================================================
# External auth adapter
# =========================================================================


def _adapter(request):
    username = request.headers.get("x-test-user")
    if not username:
        return None
    is_admin = request.headers.get("x-test-admin", "").lower() == "true"
    return ExternalAuthResult(
        user=User(id=f"ext-{username}", name=username, email=f"{username}@external.test", groups=[]),
        is_admin=is_admin,
    )


def _ext_headers(user: str, admin: bool) -> dict[str, str]:
    return {"X-Test-User": user, "X-Test-Admin": "true" if admin else "false"}


@pytest.fixture()
def ext() -> TestClient:
    plugin = CliUsersBuilder().set_auth_adapter(_adapter).build()
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    return TestClient(app)


class TestExternalAuthAdapter:
    def test_auth_endpoints_501(self, ext: TestClient):
        h = _ext_headers("alice", True)
        for res in (
            ext.post(BASE + "/sessions", json={"username": "root", "password": "x"}),
            ext.delete(BASE + "/sessions/current"),
            ext.post(BASE + "/users/x/verify-password", headers=h, json={"password": "y"}),
            ext.put(BASE + "/users/x/password", headers=h, json={"password": "yyyy"}),
        ):
            assert_error(res, 501, "host application")

    def test_admin_adapter_user(self, ext: TestClient):
        assert ext.get(BASE + "/users", headers=_ext_headers("boss", True)).status_code == 200
        assert ext.get(BASE + "/sessions", headers=_ext_headers("boss", True)).status_code == 200

    def test_non_admin_denied(self, ext: TestClient):
        assert_error(ext.get(BASE + "/sessions", headers=_ext_headers("joe", False)), 403)
        assert_error(ext.get(BASE + "/users", headers=_ext_headers("joe", False)), 403)

    def test_no_identity_401(self, ext: TestClient):
        assert_error(ext.get(BASE + "/users"), 401)

    def test_current_session(self, ext: TestClient):
        res = ext.get(BASE + "/sessions/current", headers=_ext_headers("jane", False))
        assert res.status_code == 200
        body = res.json()
        assert set(body) == {"id", "user", "permissions", "loginTime", "lastActivity", "expiresAt"}
        assert body["user"]["name"] == "jane"
        assert body["user"]["email"] == "jane@external.test"
        assert body["permissions"] == []

    def test_admin_current_has_admin_group(self, ext: TestClient):
        body = ext.get(BASE + "/sessions/current", headers=_ext_headers("boss", True)).json()
        assert body["user"]["groups"] == ["admin"]
        assert body["permissions"] == ["*"]

    def test_no_root_seed_but_groups_seeded(self, ext: TestClient):
        h = _ext_headers("boss", True)
        assert ext.get(BASE + "/users", headers=h).json() == []
        names = {g["name"] for g in ext.get(BASE + "/groups", headers=h).json()}
        assert {"admin", "users"} <= names

    def test_adapter_sessions_not_listed(self, ext: TestClient):
        h = _ext_headers("boss", True)
        ext.get(BASE + "/sessions/current", headers=h)
        assert ext.get(BASE + "/sessions", headers=h).json() == []

    def test_adapter_user_not_mutated(self):
        shared = User(id="ext-1", name="shared", email="s@x.io", groups=["users"])

        def adapter(request):
            return ExternalAuthResult(user=shared, is_admin=True)

        plugin = CliUsersBuilder().set_auth_adapter(adapter).build()
        app = FastAPI()
        app.include_router(plugin.router, prefix=plugin.prefix)
        client = TestClient(app)
        body = client.get(BASE + "/sessions/current").json()
        assert body["user"]["groups"] == ["users", "admin"]
        assert shared.groups == ["users"]

    def test_async_adapter(self):
        async def adapter(request):
            return ExternalAuthResult(user=User(id="a", name="async-user", email="a@x.io"), is_admin=True)

        plugin = CliUsersBuilder().set_auth_adapter(adapter).build()
        app = FastAPI()
        app.include_router(plugin.router, prefix=plugin.prefix)
        client = TestClient(app)
        assert client.get(BASE + "/sessions/current").json()["user"]["name"] == "async-user"


# =========================================================================
# Credentials storage
# =========================================================================


class TestCredentialFormat:
    def test_pbkdf2_format(self, api: Api, admin: str):
        user = api.create_user(admin, "alice")
        import asyncio
        creds = asyncio.run(api.plugin.credential_store.get_credentials(user["id"]))
        assert re.fullmatch(r"pbkdf2\$100000\$[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}", creds.password_hash)
