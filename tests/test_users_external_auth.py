"""External auth adapter of the users plugin: async adapters, re-checks of
events/logs sockets, adapter-granted permissions and read-only user/group
stores (mirrors the .NET ``ExternalAuth*`` / ``UsersReadOnlyStoresTests``)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliServerOptions, CliShellOptions, create_cli_server
from qodalis_cli_users import (
    CliUsersBuilder,
    ExternalAuthResult,
    Group,
    InMemoryGroupStore,
    InMemoryUserStore,
    User,
)

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


# =========================================================================
# ExternalAuthResult.permissions and read-only stores (mirrors
# cli-server-dotnet#27)
# =========================================================================

USERS_MESSAGE = "Users are managed by the host application"
GROUPS_MESSAGE = "Groups are managed by the host application"


def _csv(value: str | None) -> list[str] | None:
    return None if value is None else [p for p in value.split(",") if p]


def _header_adapter(request):
    name = request.headers.get("x-test-user")
    if not name:
        return None
    return ExternalAuthResult(
        user=User(id=f"ext-{name}", name=name, email=f"{name}@external.com",
                  groups=_csv(request.headers.get("x-test-groups")) or []),
        is_admin=request.headers.get("x-test-admin") == "true",
        permissions=_csv(request.headers.get("x-test-permissions")),
    )


def _send(client: TestClient, method: str, path: str, user: str = "alice", admin: bool = True,
          groups: str | None = None, permissions: str | None = None, body: Any = None):
    headers = {"x-test-user": user}
    if admin:
        headers["x-test-admin"] = "true"
    if groups is not None:
        headers["x-test-groups"] = groups
    if permissions is not None:
        headers["x-test-permissions"] = permissions
    if method != "GET":
        headers["origin"] = "http://testserver"
    url = path if path.startswith("/") else f"{BASE}/{path}"
    return client.request(method, url, headers=headers, json=body)


class RecordingUserStore(InMemoryUserStore):
    """Holds alice (admin) and bob; counts and fails every write."""

    def __init__(self, seeded: bool = True) -> None:
        super().__init__()
        self.writes = 0
        if seeded:
            for user in (
                User(id="ext-alice", name="alice", email="alice@external.com", groups=["admin"]),
                User(id="ext-bob", name="bob", email="bob@external.com", groups=[]),
            ):
                self._users[user.id] = user

    async def create_user(self, *args: Any, **kwargs: Any):
        self.writes += 1
        raise RuntimeError("write")

    async def update_user(self, *args: Any, **kwargs: Any):
        self.writes += 1
        raise RuntimeError("write")

    async def delete_user(self, *args: Any, **kwargs: Any):
        self.writes += 1
        raise RuntimeError("write")


class RecordingGroupStore(InMemoryGroupStore):
    """Holds admin (``*``), users and ops (``jobs:read``); counts and fails
    every write."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        for group in (
            Group(id="admin", name="admin", permissions=["*"], built_in=True),
            Group(id="users", name="users", permissions=[], built_in=True),
            Group(id="ops", name="ops", permissions=["jobs:read"]),
        ):
            self._groups[group.name] = group

    async def create_group(self, *args: Any, **kwargs: Any):
        self.writes += 1
        raise RuntimeError("write")

    async def update_group(self, *args: Any, **kwargs: Any):
        self.writes += 1
        raise RuntimeError("write")

    async def delete_group(self, *args: Any, **kwargs: Any):
        self.writes += 1
        raise RuntimeError("write")


def _app(builder: CliUsersBuilder):
    plugin = builder.build()
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    # The audit API (audit plugin), guarded by the users plugin as when
    # mounted without server protection.
    plugin.audit_plugin.authorize = plugin.authorize_audit
    app.include_router(plugin.audit_plugin.router, prefix=plugin.audit_plugin.prefix)
    return TestClient(app), plugin


def _read_only(users: bool, groups: bool):
    user_store, group_store = RecordingUserStore(), RecordingGroupStore()
    client, plugin = _app(
        CliUsersBuilder().set_auth_adapter(_header_adapter)
        .set_user_store(user_store).set_group_store(group_store)
        .set_read_only_users(users).set_read_only_groups(groups),
    )
    return client, user_store, group_store


class TestAdapterPermissions:
    def test_permissions_are_added_to_the_groups_permissions(self):
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter))
        # 'users' grants commands:execute; the result adds logs:read and
        # jobs:read; 'nope' does not exist.
        body = _send(client, "GET", "sessions/current", "bob", admin=False, groups="users,nope",
                     permissions="logs:read,jobs:read, ,LOGS:READ").json()
        assert body["permissions"] == ["commands:execute", "logs:read", "jobs:read"]
        assert body["user"]["groups"] == ["users", "nope"]

    def test_permissions_alone_authorize_routes(self):
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter))
        assert _send(client, "GET", "audit", "carol", admin=False).status_code == 403
        assert _send(client, "GET", "audit", "carol", admin=False, permissions="audit:read").status_code == 200

    def test_permissions_with_is_admin_keep_everything(self):
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter))
        body = _send(client, "GET", "sessions/current", "dana", permissions="logs:read").json()
        assert body["permissions"] == ["*", "logs:read"]

    def test_permissions_open_the_logs_socket(self, make_server):
        client, _ = make_server(_header_adapter)
        denied = {"x-test-user": "erin", "origin": ORIGIN}
        with client.websocket_connect(f"{LOGS}?ticket={_ticket(client, denied)}", headers=denied) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 4403
        allowed = {**denied, "x-test-permissions": "logs:read"}
        with client.websocket_connect(f"{LOGS}?ticket={_ticket(client, allowed)}", headers=allowed) as ws:
            _connected(ws)


USER_WRITES = [
    ("POST", "users", {"name": "new-user", "email": "new@test.com"}),
    ("PUT", "users/bob", {"email": "bob2@test.com"}),
    ("PUT", "users/alice", {"data": {"theme": "dark"}}),  # self update
    ("DELETE", "users/bob", None),
    ("POST", "groups/ops/members/bob", None),
    ("DELETE", "groups/admin/members/alice", None),
]

GROUP_WRITES = [
    ("POST", "groups", {"name": "new-group", "permissions": ["jobs:read"]}),
    ("PUT", "groups/ops", {"description": "x"}),
    ("DELETE", "groups/ops", None),
]


class TestReadOnlyStores:
    @pytest.mark.parametrize("method,path,body", USER_WRITES)
    def test_read_only_users_writes_answer_501_without_touching_the_stores(self, method, path, body):
        client, users, groups = _read_only(users=True, groups=False)
        res = _send(client, method, path, body=body)
        assert res.status_code == 501
        assert res.json() == {"error": USERS_MESSAGE}
        assert users.writes == 0 and groups.writes == 0

    @pytest.mark.parametrize("method,path,body", USER_WRITES + GROUP_WRITES)
    def test_read_only_answers_before_authentication(self, method, path, body):
        client, users, groups = _read_only(users=True, groups=True)
        res = client.request(method, f"{BASE}/{path}", json=body)
        assert res.status_code == 501
        assert res.json()["error"] in (USERS_MESSAGE, GROUPS_MESSAGE)

    def test_read_only_users_password_changes_answer_501_without_external_auth(self):
        client, plugin = _app(CliUsersBuilder().set_default_admin_password("rootpass1"))
        token = client.post(BASE + "/sessions", json={"username": "root", "password": "rootpass1"}).json()["token"]
        plugin.auth_service.options.read_only_users = True  # read on every request
        h = {"Authorization": f"Bearer {token}"}
        res = client.put(BASE + "/users/root/password", headers=h,
                         json={"password": "new-password", "currentPassword": "rootpass1"})
        assert (res.status_code, res.json()) == (501, {"error": USERS_MESSAGE})
        res = client.post(BASE + "/users", headers=h, json={"name": "x", "email": "x@test.com"})
        assert res.status_code == 501
        # Nothing changed: the old password still signs in.
        res = client.post(BASE + "/sessions", json={"username": "root", "password": "rootpass1"})
        assert res.status_code == 201

    def test_read_only_users_root_is_not_seeded(self):
        store = RecordingUserStore(seeded=False)
        client, _ = _app(CliUsersBuilder().set_user_store(store).set_read_only_users())
        res = client.post(BASE + "/sessions", json={"username": "root", "password": "root"})
        assert res.status_code == 401
        assert store.writes == 0

    def test_read_only_users_reads_still_work(self):
        client, users, _ = _read_only(users=True, groups=False)
        assert len(_send(client, "GET", "users").json()) == 2
        assert _send(client, "GET", "users/bob").status_code == 200
        assert _send(client, "GET", "groups/admin/members").status_code == 200
        assert users.writes == 0

    @pytest.mark.parametrize("method,path,body", GROUP_WRITES)
    def test_read_only_groups_writes_answer_501_without_touching_the_stores(self, method, path, body):
        client, users, groups = _read_only(users=False, groups=True)
        res = _send(client, method, path, body=body)
        assert res.status_code == 501
        assert res.json() == {"error": GROUPS_MESSAGE}
        assert users.writes == 0 and groups.writes == 0

    def test_read_only_groups_are_not_seeded_and_reads_work(self):
        store = RecordingGroupStore()
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter)
                         .set_group_store(store).set_read_only_groups())
        assert len(_send(client, "GET", "groups").json()) == 3
        assert store.writes == 0

    def test_read_only_groups_with_writable_users_keep_user_writes_and_memberships(self):
        store = RecordingGroupStore()
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter)
                         .set_group_store(store).set_read_only_groups())
        res = _send(client, "POST", "users", body={"name": "new-user", "email": "new@test.com"})
        assert res.status_code == 201, res.text
        assert _send(client, "POST", "groups/ops/members/new-user").status_code == 204
        assert store.writes == 0

    def test_read_only_users_group_delete_keeps_memberships(self):
        users = RecordingUserStore()
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter)
                         .set_user_store(users).set_read_only_users())
        assert _send(client, "POST", "groups", body={"name": "temp", "permissions": []}).status_code == 201
        users._users["ext-bob"].groups = ["temp"]
        assert _send(client, "DELETE", "groups/temp").status_code == 204
        assert users.writes == 0
        assert users._users["ext-bob"].groups == ["temp"]

    def test_writable_stores_are_unaffected(self):
        client, _ = _app(CliUsersBuilder().set_auth_adapter(_header_adapter))
        res = _send(client, "POST", "groups", body={"name": "auditors", "permissions": ["audit:read"]})
        assert res.status_code == 201
        res = _send(client, "POST", "users", body={"name": "new-user", "email": "new@test.com"})
        assert res.status_code == 201

    @pytest.mark.parametrize("users,groups", [(False, False), (True, False), (False, True), (True, True)])
    def test_current_session_publishes_the_read_only_flags(self, users, groups):
        client, _, _ = _read_only(users, groups)
        body = _send(client, "GET", "sessions/current").json()
        assert body["usersReadOnly"] is users
        assert body["groupsReadOnly"] is groups

    def test_login_publishes_the_read_only_flags(self):
        client, _ = _app(CliUsersBuilder().set_default_admin_password("rootpass1").set_read_only_groups())
        session = client.post(BASE + "/sessions", json={"username": "root", "password": "rootpass1"}).json()["session"]
        assert session["usersReadOnly"] is False
        assert session["groupsReadOnly"] is True

    def test_builder_fluent_options(self):
        builder = CliUsersBuilder()
        assert builder.set_read_only_users() is builder
        assert builder.set_read_only_groups() is builder
        options = builder.build().auth_service.options
        assert options.read_only_users and options.read_only_groups
        assert not builder.set_read_only_users(False).build().auth_service.options.read_only_users
