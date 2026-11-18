"""get_cli_caller(): host code run by an authenticated CLI request (a host processor's services) reads the caller
the auth provider established, for API tokens and sessions alike."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server, get_cli_caller
from qodalis_cli.abstractions import CliCommandProcessor
from qodalis_cli_users import CliUsersBuilder

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"


class WhoAmIProcessor(CliCommandProcessor):
    """A host command that reports who get_cli_caller() says is running it."""

    @property
    def command(self) -> str:
        return "whoami-host"

    @property
    def description(self) -> str:
        return "Reports get_cli_caller()"

    async def handle_async(self, command, cancellation_event=None) -> str:
        await asyncio.sleep(0)  # read after an await, as a host service would
        caller = get_cli_caller()
        if caller is None:
            return "user:anonymous|method:none"
        method = "token" if caller.token_id else "session"
        return f"user:{caller.user_id}|name:{caller.user_name}|method:{method}"


def _client() -> TestClient:
    result = create_cli_server(CliServerOptions(configure=lambda b: b.add_processor(WhoAmIProcessor())))
    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    result.mount_plugin(users)
    return TestClient(result.app)


def _login(c: TestClient) -> tuple[str, str]:
    res = c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS})
    assert res.status_code == 201, res.text
    body = res.json()
    return body["token"], body["session"]["user"]["id"]


def _run(c: TestClient, token: str) -> str:
    res = c.post("/api/qcli/execute", headers={"Authorization": f"Bearer {token}"}, json={"command": "whoami-host", "value": ""})
    assert res.status_code == 200, res.text
    return res.text


def test_a_host_command_sees_the_api_token_user():
    with _client() as c:
        session, user_id = _login(c)
        created = c.post(BASE + "/tokens", headers={"Authorization": f"Bearer {session}"}, json={"name": "terminal"})
        assert created.status_code == 201, created.text

        assert f"user:{user_id}|name:root|method:token" in _run(c, created.json()["token"])


def test_a_host_command_sees_the_session_user():
    with _client() as c:
        session, user_id = _login(c)

        assert f"user:{user_id}|name:root|method:session" in _run(c, session)


def test_no_caller_outside_a_request():
    assert get_cli_caller() is None
