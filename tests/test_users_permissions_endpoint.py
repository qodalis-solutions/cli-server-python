"""``GET /api/v1/qcli/permissions`` (users plugin): the permission catalog,
filtered to the caller's permissions or whole with ``all=true``."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qodalis_cli import CliPermission, CliServerOptions, create_cli_server
from .demo_processors import CliEchoCommandProcessor
from qodalis_cli_users import CliUsersBuilder
from qodalis_cli_users.permissions import USERS_PERMISSION_ENTRIES

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"

CORE_NAMES = [
    "commands:execute", "shell:execute", "http:request", "files:read", "files:write", "data:query",
    "jobs:read", "jobs:manage", "logs:read", "admin:access", "dashboard:install", "config:read", "config:write", "audit:read", "audit:clear",
]
USERS_NAMES = [
    "users:read", "users:create", "users:update", "users:delete",
    "groups:read", "groups:create", "groups:update", "groups:delete",
    "sessions:read", "sessions:revoke", "tokens:read", "tokens:revoke",
]
RATES = {"name": "rates:read", "description": "Official exchange rates", "group": "Rates", "surface": "host"}


def _h(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _error(res, status: int, text: str) -> None:
    assert res.status_code == status, res.text
    assert res.json() == {"error": text}


class Srv:
    def __init__(self, client: TestClient, users: Any) -> None:
        self.c = client
        self.users = users

    def login(self, name: str, password: str) -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": password})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    @property
    def root(self) -> str:
        return self.login("root", ROOT_PASS)

    def user(self, name: str, perms: list[str]) -> str:
        admin = self.root
        res = self.c.post(BASE + "/groups", headers=_h(admin), json={"name": f"{name}-g", "permissions": perms})
        assert res.status_code == 201, res.text
        # Only the custom group: the built-in "users" group would add commands:execute.
        res = self.c.post(BASE + "/users", headers=_h(admin), json={
            "name": name, "email": f"{name}@example.com", "password": "pass1234", "groups": [f"{name}-g"],
        })
        assert res.status_code == 201, res.text
        return self.login(name, "pass1234")

    def permissions(self, token: str | None, query: str = ""):
        return self.c.get(BASE + "/permissions" + query, headers=_h(token))


@pytest.fixture()
def srv():
    def configure(b):
        b.add_processor(CliEchoCommandProcessor())
        b.add_permissions([CliPermission(name="rates:read", description="Official exchange rates", group="Rates")])

    result = create_cli_server(CliServerOptions(configure=configure))
    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    result.mount_plugin(users)
    with TestClient(result.app) as client:
        yield Srv(client, users)


def test_users_plugin_entries_text():
    assert [e["name"] for e in USERS_PERMISSION_ENTRIES] == USERS_NAMES
    by_name = {e["name"]: e for e in USERS_PERMISSION_ENTRIES}
    assert by_name["users:read"] == {"name": "users:read", "description": "See users and their permissions", "group": "Users"}
    assert by_name["users:create"]["description"] == "Create users"
    assert by_name["users:update"]["description"] == "Change users, their groups and passwords"
    assert by_name["users:delete"]["description"] == "Delete users"
    assert by_name["groups:read"] == {"name": "groups:read", "description": "See groups", "group": "Groups"}
    assert by_name["groups:create"]["description"] == "Create groups"
    assert by_name["groups:update"]["description"] == "Change groups and their permissions"
    assert by_name["groups:delete"]["description"] == "Delete groups"
    assert by_name["sessions:read"] == {"name": "sessions:read", "description": "See every user's sessions", "group": "Sessions"}
    assert by_name["sessions:revoke"]["description"] == "End other users' sessions"
    assert by_name["tokens:read"] == {"name": "tokens:read", "description": "See every user's API tokens", "group": "Tokens"}
    assert by_name["tokens:revoke"]["description"] == "Revoke other users' API tokens"


def test_mount_adds_the_users_entries_to_the_server_catalog(srv: Srv):
    catalog = srv.users.auth_service.permission_catalog
    assert [p.name for p in catalog.all()] == [*CORE_NAMES, *USERS_NAMES, "rates:read"]
    assert catalog.find("users:read").surface == "cli"


def test_root_sees_every_entry_in_order(srv: Srv):
    res = srv.permissions(srv.root)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [e["name"] for e in body] == [*CORE_NAMES, *USERS_NAMES, "rates:read"]
    assert body[3] == {"name": "files:read", "description": "List, read and download files", "group": "Files", "surface": "cli"}
    assert list(body[3]) == ["name", "description", "group", "surface"]
    assert body[-1] == RATES


def test_caller_sees_the_entries_it_holds(srv: Srv):
    alice = srv.user("alice", ["files:*", "rates:read", "deploy:run"])
    body = srv.permissions(alice).json()
    assert [e["name"] for e in body] == ["files:read", "files:write", "rates:read"]
    assert srv.permissions(alice, "?all=false").json() == body


def test_wildcard_segments_match(srv: Srv):
    bob = srv.user("bob", ["sessions:*", "TOKENS:*", "rates"])
    names = [e["name"] for e in srv.permissions(bob).json()]
    assert names == ["sessions:read", "sessions:revoke", "tokens:read", "tokens:revoke"]


def test_all_true_with_users_read(srv: Srv):
    carol = srv.user("carol", ["users:read"])
    body = srv.permissions(carol, "?all=true").json()
    assert [e["name"] for e in body] == [*CORE_NAMES, *USERS_NAMES, "rates:read"]


def test_all_true_without_users_read(srv: Srv):
    dave = srv.user("dave", ["files:read"])
    _error(srv.permissions(dave, "?all=true"), 403, "permission denied: users:read")


@pytest.mark.parametrize("value", ["yes", "TRUE", "1", ""])
def test_all_must_be_true_or_false(srv: Srv, value: str):
    _error(srv.permissions(srv.root, f"?all={value}"), 400, "all must be true or false")


def test_no_credentials(srv: Srv):
    _error(srv.permissions(None), 401, "unauthorized")


def test_scoped_token_sees_its_scope(srv: Srv):
    root = srv.root
    res = srv.c.post(BASE + "/tokens", headers=_h(root), json={"name": "ci", "permissions": ["files:read", "rates:read"]})
    assert res.status_code == 201, res.text
    token = res.json()["token"]
    assert [e["name"] for e in srv.permissions(token).json()] == ["files:read", "rates:read"]
    _error(srv.permissions(token, "?all=true"), 403, "permission denied: users:read")


def test_password_change_pending(srv: Srv):
    erin = srv.user("erin", ["files:read"])
    erin_id = srv.c.portal.call(srv.users.user_store.get_user, "erin").id
    srv.c.portal.call(lambda: srv.users.user_store.update_user(erin_id, must_change_password=True))
    _error(srv.permissions(erin), 403, "password change required")


def test_mounting_twice_does_not_duplicate():
    result = create_cli_server()
    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).build()
    result.mount_plugin(users)
    users.on_mount(result)
    assert [p.name for p in result.builder.permissions.all()] == [*CORE_NAMES, *USERS_NAMES]


def test_standalone_router_serves_the_users_entries():
    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    app = FastAPI()
    app.include_router(users.router, prefix=users.prefix)
    with TestClient(app) as client:
        srv = Srv(client, users)
        body = srv.permissions(srv.root).json()
    assert [e["name"] for e in body] == USERS_NAMES
    assert body[0] == {"name": "users:read", "description": "See users and their permissions", "group": "Users", "surface": "cli"}
