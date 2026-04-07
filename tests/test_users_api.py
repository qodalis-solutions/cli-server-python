"""Integration tests for the Users plugin API."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Adjust sys.path so that ``plugins`` is importable.
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugins.users import build_users_plugin, UsersPluginOptions, UsersPluginResult


@pytest.fixture(scope="module")
def plugin_and_client():
    """Build a test app with the users plugin mounted."""
    app = FastAPI()

    loop = asyncio.new_event_loop()
    plugin = loop.run_until_complete(
        build_users_plugin(
            options=UsersPluginOptions(default_admin_password="admin123"),
        )
    )
    loop.close()

    app.include_router(plugin.router, prefix=plugin.prefix)
    client = TestClient(app)
    yield plugin, client
    plugin.dispose()


@pytest.fixture(scope="module")
def admin_token(plugin_and_client):
    """Login as admin and return the bearer token."""
    _, client = plugin_and_client
    res = client.post(
        "/api/v1/qcli/auth/login",
        json={"username": "root", "password": "admin123"},
    )
    assert res.status_code == 200
    return res.json()["token"]


@pytest.fixture(scope="module")
def admin_user_id(plugin_and_client):
    """Return the admin user id."""
    _, client = plugin_and_client
    res = client.post(
        "/api/v1/qcli/auth/login",
        json={"username": "root", "password": "admin123"},
    )
    return res.json()["session"]["userId"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# -------------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------------


class TestAuthLogin:
    def test_valid_credentials(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "root", "password": "admin123"},
        )
        assert res.status_code == 200
        body = res.json()
        assert "token" in body
        assert "session" in body
        assert body["session"]["userId"]

    def test_invalid_password(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "root", "password": "wrong"},
        )
        assert res.status_code == 401

    def test_unknown_user(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "nobody", "password": "pass"},
        )
        assert res.status_code == 401

    def test_missing_username(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/login",
            json={"password": "admin123"},
        )
        assert res.status_code == 422  # Pydantic validation

    def test_missing_password(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "root"},
        )
        assert res.status_code == 422


class TestAuthLogout:
    def test_logout_invalidates_session(self, plugin_and_client):
        _, client = plugin_and_client
        login_res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "root", "password": "admin123"},
        )
        temp_token = login_res.json()["token"]

        logout_res = client.post(
            "/api/v1/qcli/auth/logout",
            headers=_auth(temp_token),
        )
        assert logout_res.status_code == 204

        # Token should no longer work
        users_res = client.get(
            "/api/v1/qcli/users",
            headers=_auth(temp_token),
        )
        assert users_res.status_code == 401

    def test_logout_without_token(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post("/api/v1/qcli/auth/logout")
        assert res.status_code == 401


class TestAuthVerify:
    def test_correct_password(self, plugin_and_client, admin_user_id):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/verify",
            json={"userId": admin_user_id, "password": "admin123"},
        )
        assert res.status_code == 200
        assert res.json()["valid"] is True

    def test_wrong_password(self, plugin_and_client, admin_user_id):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/verify",
            json={"userId": admin_user_id, "password": "wrong"},
        )
        assert res.status_code == 200
        assert res.json()["valid"] is False

    def test_missing_user_id(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/auth/verify",
            json={"password": "admin123"},
        )
        assert res.status_code == 422


# -------------------------------------------------------------------------
# Users
# -------------------------------------------------------------------------


class TestUsersEndpoints:
    def test_list_users_requires_auth(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/users")
        assert res.status_code == 401

    def test_list_users(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/users", headers=_auth(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)
        assert len(res.json()) > 0

    def test_create_user(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/users",
            headers=_auth(admin_token),
            json={"name": "alice", "email": "alice@example.com", "password": "alicepass"},
        )
        assert res.status_code == 201
        assert res.json()["name"] == "alice"
        assert "id" in res.json()

    def test_duplicate_username(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/users",
            headers=_auth(admin_token),
            json={"name": "alice", "email": "alice2@example.com"},
        )
        assert res.status_code == 409

    def test_create_user_missing_name(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/users",
            headers=_auth(admin_token),
            json={"email": "noemail@example.com"},
        )
        assert res.status_code == 422

    def test_get_user_by_name(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/users/root", headers=_auth(admin_token))
        assert res.status_code == 200
        assert res.json()["name"] == "root"

    def test_get_user_not_found(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/users/no-such-user", headers=_auth(admin_token))
        assert res.status_code == 404

    def test_update_user(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        # Get alice's id
        get_res = client.get("/api/v1/qcli/users/alice", headers=_auth(admin_token))
        alice_id = get_res.json()["id"]

        res = client.put(
            f"/api/v1/qcli/users/{alice_id}",
            headers=_auth(admin_token),
            json={"email": "alice-updated@example.com"},
        )
        assert res.status_code == 200
        assert res.json()["email"] == "alice-updated@example.com"

    def test_delete_user(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        # Create a user to delete
        create_res = client.post(
            "/api/v1/qcli/users",
            headers=_auth(admin_token),
            json={"name": "deleteme", "email": "deleteme@example.com"},
        )
        user_id = create_res.json()["id"]

        delete_res = client.delete(
            f"/api/v1/qcli/users/{user_id}",
            headers=_auth(admin_token),
        )
        assert delete_res.status_code == 204

        get_res = client.get(
            f"/api/v1/qcli/users/{user_id}",
            headers=_auth(admin_token),
        )
        assert get_res.status_code == 404


class TestPasswordChange:
    def test_admin_can_change_others_password(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        get_res = client.get("/api/v1/qcli/users/alice", headers=_auth(admin_token))
        alice_id = get_res.json()["id"]

        res = client.put(
            f"/api/v1/qcli/users/{alice_id}/password",
            headers=_auth(admin_token),
            json={"newPassword": "newAlicePass"},
        )
        assert res.status_code == 200
        assert res.json()["message"]

    def test_user_can_change_own_password(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        # Login as alice
        login_res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "alice", "password": "newAlicePass"},
        )
        alice_token = login_res.json()["token"]
        alice_id = login_res.json()["session"]["userId"]

        res = client.put(
            f"/api/v1/qcli/users/{alice_id}/password",
            headers=_auth(alice_token),
            json={"currentPassword": "newAlicePass", "newPassword": "alicepass2"},
        )
        assert res.status_code == 200

    def test_self_change_wrong_current_password(self, plugin_and_client):
        _, client = plugin_and_client
        login_res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "alice", "password": "alicepass2"},
        )
        alice_token = login_res.json()["token"]
        alice_id = login_res.json()["session"]["userId"]

        res = client.put(
            f"/api/v1/qcli/users/{alice_id}/password",
            headers=_auth(alice_token),
            json={"currentPassword": "wrongcurrent", "newPassword": "newpass"},
        )
        assert res.status_code == 401


# -------------------------------------------------------------------------
# Sessions
# -------------------------------------------------------------------------


class TestSessions:
    def test_admin_can_list_sessions(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/sessions", headers=_auth(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_current_session(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/sessions/current", headers=_auth(admin_token))
        assert res.status_code == 200
        assert res.json()["token"] == admin_token

    def test_kick_session(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        # Login as alice to get a session
        login_res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "alice", "password": "alicepass2"},
        )
        alice_token = login_res.json()["token"]
        alice_session_id = login_res.json()["session"]["id"]

        # Admin kicks alice's session
        kick_res = client.delete(
            f"/api/v1/qcli/sessions/{alice_session_id}",
            headers=_auth(admin_token),
        )
        assert kick_res.status_code == 204

        # Alice's token should no longer work
        users_res = client.get(
            "/api/v1/qcli/users",
            headers=_auth(alice_token),
        )
        assert users_res.status_code == 401

    def test_kick_nonexistent_session(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.delete(
            "/api/v1/qcli/sessions/no-such-session-id",
            headers=_auth(admin_token),
        )
        assert res.status_code == 404


# -------------------------------------------------------------------------
# Groups
# -------------------------------------------------------------------------


class TestGroups:
    def test_create_group(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/groups",
            headers=_auth(admin_token),
            json={"name": "editors", "description": "Content editors"},
        )
        assert res.status_code == 201
        assert res.json()["name"] == "editors"

    def test_duplicate_group(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/groups",
            headers=_auth(admin_token),
            json={"name": "editors"},
        )
        assert res.status_code == 409

    def test_list_groups(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/groups", headers=_auth(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_get_group(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/groups/editors", headers=_auth(admin_token))
        assert res.status_code == 200
        assert res.json()["name"] == "editors"

    def test_get_group_not_found(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/groups/no-such-group", headers=_auth(admin_token))
        assert res.status_code == 404

    def test_add_member(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        get_res = client.get("/api/v1/qcli/users/alice", headers=_auth(admin_token))
        alice_id = get_res.json()["id"]

        res = client.put(
            f"/api/v1/qcli/groups/editors/members/{alice_id}",
            headers=_auth(admin_token),
        )
        assert res.status_code == 200

    def test_remove_member(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        get_res = client.get("/api/v1/qcli/users/alice", headers=_auth(admin_token))
        alice_id = get_res.json()["id"]

        res = client.delete(
            f"/api/v1/qcli/groups/editors/members/{alice_id}",
            headers=_auth(admin_token),
        )
        assert res.status_code == 200

    def test_delete_group(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        # Create a disposable group
        client.post(
            "/api/v1/qcli/groups",
            headers=_auth(admin_token),
            json={"name": "tempgroup"},
        )

        res = client.delete(
            "/api/v1/qcli/groups/tempgroup",
            headers=_auth(admin_token),
        )
        assert res.status_code == 204


# -------------------------------------------------------------------------
# Audit
# -------------------------------------------------------------------------


class TestAudit:
    def test_admin_can_list_audit(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/audit", headers=_auth(admin_token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)
        assert len(res.json()) > 0

    def test_filter_by_event(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/audit?event=login", headers=_auth(admin_token))
        assert res.status_code == 200
        for entry in res.json():
            assert entry["event"] == "login"

    def test_audit_requires_auth(self, plugin_and_client):
        _, client = plugin_and_client
        res = client.get("/api/v1/qcli/audit")
        assert res.status_code == 401

    def test_clear_audit(self, plugin_and_client, admin_token):
        _, client = plugin_and_client
        res = client.delete("/api/v1/qcli/audit", headers=_auth(admin_token))
        assert res.status_code == 204

        get_res = client.get("/api/v1/qcli/audit", headers=_auth(admin_token))
        assert len(get_res.json()) == 0


# -------------------------------------------------------------------------
# Non-admin restrictions
# -------------------------------------------------------------------------


class TestNonAdminRestrictions:
    @pytest.fixture(scope="class")
    def bob_setup(self, plugin_and_client, admin_token):
        """Create bob (non-admin) and return his token and ids."""
        _, client = plugin_and_client
        # Admin creates bob
        client.post(
            "/api/v1/qcli/users",
            headers=_auth(admin_token),
            json={"name": "bob", "email": "bob@example.com", "password": "bobpass"},
        )
        # Bob logs in
        login_res = client.post(
            "/api/v1/qcli/auth/login",
            json={"username": "bob", "password": "bobpass"},
        )
        data = login_res.json()
        return {
            "token": data["token"],
            "user_id": data["session"]["userId"],
            "session_id": data["session"]["id"],
        }

    def test_cannot_create_users(self, plugin_and_client, bob_setup):
        _, client = plugin_and_client
        res = client.post(
            "/api/v1/qcli/users",
            headers=_auth(bob_setup["token"]),
            json={"name": "charlie", "email": "charlie@example.com"},
        )
        assert res.status_code == 403

    def test_cannot_list_sessions(self, plugin_and_client, bob_setup):
        _, client = plugin_and_client
        res = client.get(
            "/api/v1/qcli/sessions",
            headers=_auth(bob_setup["token"]),
        )
        assert res.status_code == 403

    def test_can_get_own_session(self, plugin_and_client, bob_setup):
        _, client = plugin_and_client
        res = client.get(
            "/api/v1/qcli/sessions/current",
            headers=_auth(bob_setup["token"]),
        )
        assert res.status_code == 200
        assert res.json()["id"] == bob_setup["session_id"]

    def test_cannot_access_audit(self, plugin_and_client, bob_setup):
        _, client = plugin_and_client
        res = client.get(
            "/api/v1/qcli/audit",
            headers=_auth(bob_setup["token"]),
        )
        assert res.status_code == 403

    def test_cannot_delete_groups(self, plugin_and_client, bob_setup):
        _, client = plugin_and_client
        res = client.delete(
            "/api/v1/qcli/groups/admin",
            headers=_auth(bob_setup["token"]),
        )
        assert res.status_code == 403

    def test_cannot_change_others_password(self, plugin_and_client, bob_setup, admin_user_id):
        _, client = plugin_and_client
        res = client.put(
            f"/api/v1/qcli/users/{admin_user_id}/password",
            headers=_auth(bob_setup["token"]),
            json={"newPassword": "hackedpass"},
        )
        assert res.status_code == 403
