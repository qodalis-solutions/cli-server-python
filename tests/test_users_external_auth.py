"""External auth adapter of the users plugin: async adapters, re-checks of
events/logs sockets, adapter-granted permissions and read-only user/group
stores (mirrors the .NET ``ExternalAuth*`` / ``UsersReadOnlyStoresTests``)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliServerOptions, CliShellOptions, create_cli_server
from qodalis_cli_users import CliUsersBuilder, ExternalAuthResult, User

BASE = "/api/v1/qcli"
ORIGIN = "http://app.test"
SHELL = "/ws/qcli/shell"
EVENTS = "/ws/qcli/events"
LOGS = "/ws/qcli/logs"


def _user(name: str, groups: list[str] | None = None, disabled: bool = False) -> User:
    return User(id=f"ext-{name}", name=name, email=f"{name}@x.test", groups=list(groups or []), disabled=disabled)


@pytest.fixture()
def make_server():
    """``factory(adapter, builder=None, **shell) -> (client, users)``: a CLI
    server (shell enabled) protected by the users plugin in adapter mode,
    started like a host app would (``startup()``)."""
    started: list[tuple[TestClient, Any]] = []

    def factory(adapter: Callable, builder: CliUsersBuilder | None = None, **shell: Any):
        shell.setdefault("shell", "/bin/sh")
        result = create_cli_server(CliServerOptions(
            enable_shell=True, shell=CliShellOptions(**shell), allowed_origins=[ORIGIN],
        ))
        users = (builder or CliUsersBuilder()).set_auth_adapter(adapter).build()
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


def _headers(name: str = "ext") -> dict[str, str]:
    return {"x-user": name, "origin": ORIGIN}


def _ticket(client: TestClient, headers: dict[str, str]) -> str:
    res = client.post(BASE + "/sessions/ws-ticket", headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["ticket"]


def _connect(client: TestClient, path: str, headers: dict[str, str] | None = None):
    headers = headers or _headers()
    return client.websocket_connect(f"{path}?ticket={_ticket(client, headers)}", headers=headers)


def _until_closed(ws) -> WebSocketDisconnect:
    while True:
        try:
            ws.receive_text()
        except WebSocketDisconnect as exc:
            return exc


def _ready(ws) -> None:
    assert json.loads(ws.receive_text())["type"] == "ready"


def _connected(ws) -> None:
    assert json.loads(ws.receive_text())["type"] == "connected"


# =========================================================================
# Async adapter (mirrors cli-server-dotnet#26)
# =========================================================================


class TestAsyncAdapter:
    @staticmethod
    def _async_adapter(state: dict[str, Any]):
        async def adapter(request):
            await asyncio.sleep(0)  # e.g. a database lookup
            state["calls"] = state.get("calls", 0) + 1
            name = request.headers.get("x-user")
            if not name or state.get("mode") == "none":
                return None
            return ExternalAuthResult(user=_user(name, state.get("groups", ["shellers"])))

        return adapter

    def test_async_adapter_authenticates_requests(self, make_server):
        state: dict[str, Any] = {}
        client, _ = make_server(self._async_adapter(state))
        res = client.get(BASE + "/sessions/current", headers=_headers("alice"))
        assert res.status_code == 200, res.text
        assert res.json()["user"]["name"] == "alice"
        assert state["calls"] == 1
        assert client.get(BASE + "/sessions/current").status_code == 401

    def test_async_callable_object(self, make_server):
        class HostAdapter:
            async def __call__(self, request):
                await asyncio.sleep(0)
                return ExternalAuthResult(user=_user("obj"), is_admin=True)

        client, _ = make_server(HostAdapter())
        body = client.get(BASE + "/sessions/current").json()
        assert body["user"]["name"] == "obj"
        assert body["permissions"] == ["*"]

    def test_sync_adapter_returning_awaitable(self, make_server):
        async def load(name: str):
            return ExternalAuthResult(user=_user(name))

        client, _ = make_server(lambda request: load("lazy"))
        assert client.get(BASE + "/sessions/current").json()["user"]["name"] == "lazy"

    def test_async_adapter_issues_tickets_for_sockets(self, make_server):
        client, _ = make_server(self._async_adapter({}))
        with _connect(client, EVENTS) as ws:
            _connected(ws)

    def test_async_adapter_shell_rechecks(self, make_server):
        state: dict[str, Any] = {}
        client, users = make_server(self._async_adapter(state), session_check_interval=0.2)
        client.portal.call(users.group_store.create_group, "shellers", "", ["shell:execute"])
        with _connect(client, SHELL) as ws:
            _ready(ws)
            calls = state["calls"]
            time.sleep(0.7)  # several periodic re-checks through the async adapter
            assert state["calls"] > calls
            state["mode"] = "none"
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")

    def test_non_callable_adapter_rejected(self):
        with pytest.raises(TypeError):
            CliUsersBuilder().set_auth_adapter("not an adapter")  # type: ignore[arg-type]
