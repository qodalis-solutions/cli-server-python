"""API tokens next to an external auth adapter (``set_allow_api_tokens()``):
the host signs users in, and the plugin's tokens (created by those users or
handed to a linked terminal) authenticate on their own (mirrors the .NET
``ExternalAuthApiTokensTests``)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_users import CliUsersBuilder, ExternalAuthResult, InMemoryUserStore

BASE = "/api/v1/qcli"
#: Same origin as the test client: adapter auth needs it on writes (CSRF).
ORIGIN = "http://testserver"


@pytest.fixture()
def start():
    """``factory(allow_api_tokens) -> (client, users)``: a CLI server
    protected by the users plugin whose adapter signs in the store's user
    named in ``X-User``."""
    started: list[tuple[TestClient, Any]] = []

    def factory(allow_api_tokens: bool):
        store = InMemoryUserStore()

        async def adapter(request):
            name = request.headers.get("x-user")
            user = await store.get_user(name) if name else None
            return ExternalAuthResult(user=user) if user is not None else None

        builder = CliUsersBuilder().set_user_store(store).set_auth_adapter(adapter)
        if allow_api_tokens:
            builder.set_allow_api_tokens()
        users = builder.build()
        result = create_cli_server(CliServerOptions())
        result.mount_plugin(users)
        client = TestClient(result.app)
        client.__enter__()
        client.portal.call(users.startup)
        started.append((client, users))
        return client, users

    yield factory
    for client, users in started:
        client.portal.call(users.shutdown)
        client.__exit__(None, None, None)


def _create_user(client: TestClient, users: Any, name: str) -> str:
    async def create():
        return await users.user_store.create_user(name, f"{name}@external.com", ["users"])

    return client.portal.call(create).id


def _as_user(client: TestClient, method: str, path: str, user: str, body: Any = None):
    return client.request(method, f"{BASE}/{path}", json=body, headers={"x-user": user, "origin": ORIGIN})


def _with_token(client: TestClient, path: str, token: str, method: str = "GET", body: Any = None):
    return client.request(method, f"{BASE}/{path}", json=body, headers={"authorization": f"Bearer {token}"})


def test_a_user_signed_in_by_the_host_creates_a_token_that_authenticates_on_its_own(start):
    client, users = start(allow_api_tokens=True)
    user_id = _create_user(client, users, "ann")

    created = _as_user(client, "POST", "tokens", "ann", {"name": "ci"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    listed = _with_token(client, "tokens", token)

    assert token.startswith("qcli_pat_")
    assert listed.status_code == 200, listed.text
    assert [t["name"] for t in listed.json()] == ["ci"]
    assert created.json()["apiToken"]["userId"] == user_id
    # A token never creates another one
    again = _with_token(client, "tokens", token, "POST", {"name": "again"})
    assert again.status_code == 403, again.text


def test_a_token_stops_working_when_the_store_disables_its_user(start):
    client, users = start(allow_api_tokens=True)
    user_id = _create_user(client, users, "bob")
    token = _as_user(client, "POST", "tokens", "bob", {"name": "ci"}).json()["token"]

    async def disable():
        await users.user_store.update_user(user_id, disabled=True)

    client.portal.call(disable)

    assert _with_token(client, "tokens", token).status_code == 401


def test_a_user_signed_in_by_the_host_approves_a_link_and_the_terminal_uses_its_token(start):
    client, users = start(allow_api_tokens=True)
    _create_user(client, users, "cara")
    created = client.post(f"{BASE}/link", json={"client": "pwa"})
    assert created.status_code == 201, created.text
    code = created.json()["userCode"]

    approved = _as_user(client, "POST", f"link/code/{code}/approve", "cara", {})
    collected = client.get(f"{BASE}/link/{created.json()['linkId']}")
    token = collected.json()["token"]

    assert approved.status_code == 200, approved.text
    assert _with_token(client, "tokens", token).status_code == 200


def test_without_the_option_tokens_stay_off_and_bearer_tokens_go_to_the_adapter(start):
    client, users = start(allow_api_tokens=False)
    _create_user(client, users, "dan")

    refused = _as_user(client, "POST", "tokens", "dan", {"name": "ci"})
    assert refused.status_code == 501, refused.text
    assert refused.json() == {"error": "Authentication is managed by the host application"}
    assert _with_token(client, "tokens", "qcli_pat_not-a-real-token").status_code == 401
