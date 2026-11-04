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
        client.cli = result  # type: ignore[attr-defined]
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


# =========================================================================
# Events/logs sockets re-checked through the adapter (mirrors
# cli-server-dotnet#25)
# =========================================================================


class HostState:
    """What the host says about each user: ``admin`` (``*``), ``user`` (no
    permissions), ``gone`` (``None``), ``other`` (another user),
    ``disabled``, ``throw``."""

    def __init__(self, **users: str) -> None:
        self.users = dict(users)
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.delay = 0.0

    async def __call__(self, request):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            name = request.headers.get("x-user")
            status = self.users.get(name or "")
            if status is None or status == "gone":
                return None
            if status == "throw":
                raise RuntimeError("identity provider down")
            if status == "other":
                return ExternalAuthResult(user=_user(name + "-other"), is_admin=True)
            return ExternalAuthResult(
                user=_user(name, disabled=status == "disabled"),
                is_admin=status in ("admin", "disabled"),
            )
        finally:
            self.active -= 1


def _open(client: TestClient, path: str, name: str):
    ws = _connect(client, path, _headers(name))
    socket = ws.__enter__()
    _connected(socket)
    return ws, socket


def _close(ws) -> None:
    ws.__exit__(None, None, None)


class TestSocketRecheck:
    INTERVAL = 0.2

    @pytest.mark.parametrize("path,status", [
        (EVENTS, "gone"), (EVENTS, "other"), (EVENTS, "disabled"), (EVENTS, "throw"),
        (LOGS, "gone"), (LOGS, "other"), (LOGS, "disabled"), (LOGS, "throw"),
        (LOGS, "user"),  # no longer holds logs:read
    ])
    def test_socket_closes_when_the_host_no_longer_authenticates(self, make_server, path, status):
        state = HostState(alice="admin")
        client, _ = make_server(state, session_check_interval=self.INTERVAL)
        ws, socket = _open(client, path, "alice")
        state.users["alice"] = status
        exc = _until_closed(socket)
        assert (exc.code, exc.reason) == (4401, "session ended")
        _close(ws)

    def test_events_socket_without_permissions_stays_open(self, make_server):
        state = HostState(bob="user")
        client, _ = make_server(state, session_check_interval=self.INTERVAL)
        ws, socket = _open(client, EVENTS, "bob")
        calls = state.calls
        time.sleep(0.7)  # several re-checks
        assert state.calls > calls
        # Events need no permission: still open and delivering.
        client.portal.call(client.cli.event_socket_manager.broadcast_message, json.dumps({"type": "marker"}))
        assert json.loads(socket.receive_text())["type"] == "marker"
        _close(ws)

    def test_logs_socket_stays_open_while_authenticated(self, make_server):
        state = HostState(carol="admin")
        client, users = make_server(state, session_check_interval=self.INTERVAL)
        ws, socket = _open(client, LOGS, "carol")
        calls = state.calls
        time.sleep(0.7)
        assert state.calls >= calls + 2
        client.portal.call(users.recheck_external_sockets)

        async def emit() -> None:
            client.cli.log_socket_manager.broadcast_log("error", "still-open")
            await asyncio.sleep(0.05)

        client.portal.call(emit)
        while "still-open" not in socket.receive_text():
            pass
        _close(ws)

    def test_socket_is_rechecked_with_the_ticket_request(self, make_server):
        """The upgrade carries no identity header: the adapter is re-run on
        the snapshot of the ``ws-ticket`` request."""
        state = HostState(dave="admin")
        client, _ = make_server(state, session_check_interval=self.INTERVAL)
        ticket = _ticket(client, _headers("dave"))
        with client.websocket_connect(f"{LOGS}?ticket={ticket}", headers={"origin": ORIGIN}) as socket:
            _connected(socket)
            time.sleep(0.5)  # re-checks pass on the ticket request
            state.users["dave"] = "gone"
            exc = _until_closed(socket)
            assert (exc.code, exc.reason) == (4401, "session ended")

    def test_only_the_affected_users_sockets_close(self, make_server):
        state = HostState(erin="admin", frank="admin")
        client, _ = make_server(state, session_check_interval=self.INTERVAL)
        erin_ws, erin = _open(client, EVENTS, "erin")
        frank_ws, frank = _open(client, EVENTS, "frank")
        state.users["erin"] = "gone"
        assert _until_closed(erin).code == 4401
        client.portal.call(client.cli.event_socket_manager.broadcast_message, json.dumps({"type": "marker"}))
        assert json.loads(frank.receive_text())["type"] == "marker"
        _close(frank_ws)
        _close(erin_ws)

    def test_slow_adapter_checks_do_not_stack(self, make_server):
        state = HostState(gail="admin")
        client, _ = make_server(state, session_check_interval=0.05)
        ws, socket = _open(client, EVENTS, "gail")
        state.delay = 0.3  # six intervals per check
        state.max_active = 0
        time.sleep(1.0)
        assert state.max_active == 1
        _close(ws)

    def test_disabled_user_is_refused_on_requests(self, make_server):
        client, _ = make_server(HostState(gina="disabled"))
        res = client.get(BASE + "/sessions/current", headers=_headers("gina"))
        assert res.status_code == 401
        assert res.json() == {"error": "account disabled"}
        res = client.post(BASE + "/sessions/ws-ticket", headers=_headers("gina"))
        assert res.status_code == 401
        assert res.json() == {"error": "account disabled"}

    def test_disabled_user_closes_the_shell(self, make_server):
        state = HostState(hank="admin")
        client, _ = make_server(state, session_check_interval=self.INTERVAL)
        with _connect(client, SHELL, _headers("hank")) as ws:
            _ready(ws)
            state.users["hank"] = "disabled"
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")

    def test_check_interval_follows_session_check_interval_capped_at_60s(self):
        def interval(value: float | None) -> float:
            shell = CliShellOptions() if value is None else CliShellOptions(session_check_interval=value)
            result = create_cli_server(CliServerOptions(shell=shell))
            users = CliUsersBuilder().set_auth_adapter(HostState()).build()
            result.mount_plugin(users)
            return users.external_socket_check_interval

        assert interval(None) == 30
        assert interval(5) == 5
        assert interval(600) == 60
        assert interval(0) == 30
