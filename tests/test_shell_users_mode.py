"""Remote shell security (Appendix C) with the users plugin: users mode,
session binding, per-user limits and audit."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliServerOptions, CliShellOptions, create_cli_server
from qodalis_cli_users import AuditQueryOptions, CliUsersBuilder

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
SHELL = "/ws/qcli/shell"


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Env:
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

    def user(self, name: str, perms: list[str]) -> str:
        admin = self.root
        group = f"{name}-g"
        res = self.c.post(BASE + "/groups", headers=_h(admin), json={"name": group, "permissions": perms})
        assert res.status_code == 201, res.text
        res = self.c.post(BASE + "/users", headers=_h(admin), json={
            "name": name, "email": f"{name}@example.com", "password": "pass1234", "groups": [group, "users"],
        })
        assert res.status_code == 201, res.text
        return self.login(name, "pass1234")

    def ticket(self, token: str) -> str:
        res = self.c.post(BASE + "/sessions/ws-ticket", headers=_h(token))
        assert res.status_code == 201, res.text
        return res.json()["ticket"]

    def shell_url(self, token: str, extra: str = "") -> str:
        return f"{SHELL}?ticket={self.ticket(token)}{extra}"

    def session_id(self, token: str) -> str:
        return self.c.get(BASE + "/sessions/current", headers=_h(token)).json()["id"]

    def audit(self, event: str) -> list:
        return self.c.portal.call(
            self.users.audit_store.get_entries, AuditQueryOptions(event=event),
        )


@pytest.fixture()
def make_env():
    stack = []

    def factory(users_builder: CliUsersBuilder | None = None, **shell: Any) -> Env:
        shell.setdefault("shell", "/bin/sh")
        result = create_cli_server(CliServerOptions(enable_shell=True, shell=CliShellOptions(**shell)))
        users = (users_builder or CliUsersBuilder()).set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
        result.mount_plugin(users)
        client = TestClient(result.app)
        client.__enter__()
        stack.append(client)
        return Env(client, users, result)

    yield factory
    for client in stack:
        client.__exit__(None, None, None)


def _ready(ws) -> None:
    assert json.loads(ws.receive_text())["type"] == "ready"


def _until_closed(ws) -> WebSocketDisconnect:
    while True:
        try:
            ws.receive_text()
        except WebSocketDisconnect as exc:
            return exc


def _rejected(client: TestClient, url: str, headers: dict | None = None) -> WebSocketDisconnect:
    with client.websocket_connect(url, headers=headers or {}) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    return exc.value


class TestUsersMode:
    def test_capabilities_report_users(self, make_env):
        env = make_env()
        caps = env.c.get("/api/qcli/capabilities").json()
        assert caps["shell"] is True and caps["shellAuth"] == "users"

    def test_ticket_and_permission_required(self, make_env):
        env = make_env()
        assert _rejected(env.c, SHELL).code == 4401
        plain = env.user("alice", ["commands:execute"])
        assert _rejected(env.c, env.shell_url(plain)).code == 4403
        with env.c.websocket_connect(env.shell_url(env.root)) as ws:
            _ready(ws)

    def test_without_server_protection_ticket_still_required(self, make_env):
        env = make_env(CliUsersBuilder().set_protect_server(False))
        exc = _rejected(env.c, SHELL)
        assert (exc.code, exc.reason) == (4401, "login required")
        plain = env.user("alice", ["commands:execute"])
        assert _rejected(env.c, env.shell_url(plain)).code == 4403
        with env.c.websocket_connect(env.shell_url(env.root)) as ws:
            _ready(ws)

    def test_token_also_required_when_configured(self, make_env):
        env = make_env(token="extra-secret")
        assert _rejected(env.c, env.shell_url(env.root)).code == 4401
        url = env.shell_url(env.root, "&_header_Authorization=Bearer%20extra-secret")
        with env.c.websocket_connect(url) as ws:
            _ready(ws)

    def test_audit_opened_and_closed_without_command_text(self, make_env):
        env = make_env()
        root = env.root
        sid = env.session_id(root)
        with env.c.websocket_connect(env.shell_url(root, "&cmd=echo%20SENTINEL_CMD%3B%20exit%203")) as ws:
            _ready(ws)
            for _ in range(200):
                msg = json.loads(ws.receive_text())
                if msg["type"] == "exit":
                    assert msg["code"] == 3
                    break
        with env.c.websocket_connect(env.shell_url(root)) as ws:
            _ready(ws)
            ws.send_text(json.dumps({"type": "stdin", "data": "echo SENTINEL_STDIN\n"}))
            time.sleep(0.2)

        opened = env.audit("shell_opened")
        closed = env.audit("shell_closed")
        assert len(opened) == 2 and len(closed) == 2
        user_id = opened[0].user_id
        assert user_id and all(e.user_id == user_id and e.actor_id == user_id for e in opened + closed)
        assert {e.metadata["mode"] for e in opened} == {"command", "interactive"}
        assert all(e.metadata["sessionId"] == sid for e in opened + closed)
        by_mode = {e.metadata["mode"]: e.metadata for e in closed}
        assert by_mode["command"]["exitCode"] == 3
        assert by_mode["command"]["reason"] == "exited"
        assert by_mode["interactive"]["reason"] == "closed"
        assert "exitCode" not in by_mode["interactive"]
        assert all(isinstance(e.metadata["durationMs"], int) for e in closed)
        dump = repr(opened + closed)
        assert "SENTINEL" not in dump and "echo" not in dump

    def test_logout_closes_shell(self, make_env):
        env = make_env()
        token = env.login("root", ROOT_PASS)
        with env.c.websocket_connect(env.shell_url(token)) as ws:
            _ready(ws)
            assert env.c.delete(BASE + "/sessions/current", headers=_h(token)).status_code == 204
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")
        closed = env.audit("shell_closed")
        assert closed[-1].metadata["reason"] == "session ended"
        assert "exitCode" not in closed[-1].metadata
        assert env.result.shell_session_manager.active_sessions == 0

    def test_kick_closes_only_that_session(self, make_env):
        env = make_env()
        alice = env.user("alice", ["shell:execute"])
        alice2 = env.login("alice", "pass1234")
        root = env.root
        with env.c.websocket_connect(env.shell_url(alice)) as a, \
                env.c.websocket_connect(env.shell_url(alice2)) as b:
            _ready(a)
            _ready(b)
            sid = env.session_id(alice)
            assert env.c.delete(f"{BASE}/sessions/{sid}", headers=_h(root)).status_code == 204
            assert _until_closed(a).code == 4401
            b.send_text(json.dumps({"type": "stdin", "data": "echo b-alive\n"}))
            out = ""
            while "b-alive" not in out.replace("echo b-alive", ""):
                out += json.loads(b.receive_text()).get("data", "")

    def test_permission_removal_closes_shell(self, make_env):
        env = make_env()
        alice = env.user("alice", ["shell:execute"])
        with env.c.websocket_connect(env.shell_url(alice)) as ws:
            _ready(ws)
            res = env.c.put(BASE + "/groups/alice-g", headers=_h(env.root), json={"permissions": ["commands:execute"]})
            assert res.status_code == 200, res.text
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")
        assert env.audit("shell_closed")[-1].metadata["reason"] == "permission removed"

    def test_disable_user_closes_shell(self, make_env):
        env = make_env()
        alice = env.user("alice", ["shell:execute"])
        with env.c.websocket_connect(env.shell_url(alice)) as ws:
            _ready(ws)
            res = env.c.put(BASE + "/users/alice", headers=_h(env.root), json={"disabled": True})
            assert res.status_code == 200, res.text
            assert _until_closed(ws).code == 4401

    def test_delete_user_closes_shell(self, make_env):
        env = make_env()
        alice = env.user("alice", ["shell:execute"])
        with env.c.websocket_connect(env.shell_url(alice)) as ws:
            _ready(ws)
            assert env.c.delete(BASE + "/users/alice", headers=_h(env.root)).status_code == 204
            assert _until_closed(ws).code == 4401

    def test_periodic_recheck_without_events(self, make_env):
        env = make_env(session_check_interval=0.3)
        alice = env.user("alice", ["shell:execute"])
        sid = env.session_id(alice)
        with env.c.websocket_connect(env.shell_url(alice)) as ws:
            _ready(ws)
            # Expire the session behind the plugin's back (no audit event).
            env.c.portal.call(env.users.session_store.delete_session_by_id, sid)
            start = time.monotonic()
            assert _until_closed(ws).code == 4401
            assert time.monotonic() - start < 3

    def test_check_interval_is_capped_at_60s(self, make_env):
        env = make_env(session_check_interval=3600)
        assert env.result.shell_session_manager.session_check_interval == 60
        env = make_env(session_check_interval=0)
        assert env.result.shell_session_manager.session_check_interval == 30
        env = make_env()
        assert env.result.shell_session_manager.session_check_interval == 30

    def test_per_user_limit(self, make_env):
        env = make_env(max_sessions_per_user=1)
        alice = env.user("alice", ["shell:execute"])
        bob = env.user("bob", ["shell:execute"])
        with env.c.websocket_connect(env.shell_url(alice)) as a:
            _ready(a)
            exc = _rejected(env.c, env.shell_url(env.login("alice", "pass1234")))
            assert (exc.code, exc.reason) == (4429, "too many shell sessions")
            with env.c.websocket_connect(env.shell_url(bob)) as b:
                _ready(b)

    def test_idle_timeout_in_users_mode(self, make_env):
        env = make_env(idle_timeout=0.5)
        with env.c.websocket_connect(env.shell_url(env.root)) as ws:
            _ready(ws)
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4408, "shell timed out")
        assert env.audit("shell_closed")[-1].metadata["reason"] == "shell timed out"

    def test_periodic_check_does_not_extend_session(self, make_env):
        env = make_env(session_check_interval=0.1)
        alice = env.user("alice", ["shell:execute"])
        sid = env.session_id(alice)

        def last_activity():
            return env.c.portal.call(env.users.session_store.get_session_by_id, sid).last_activity

        with env.c.websocket_connect(env.shell_url(alice)) as ws:
            _ready(ws)
            before = last_activity()
            time.sleep(0.5)  # several re-checks without stdin
            assert last_activity() == before

    def test_stdin_touches_session_at_most_every_30s(self, make_env, monkeypatch):
        import qodalis_cli.services.cli_shell_session_manager as mod
        monkeypatch.setattr(mod, "TOUCH_INTERVAL", 0.2)
        env = make_env(session_check_interval=0.1)
        alice = env.user("alice", ["shell:execute"])
        sid = env.session_id(alice)

        def last_activity():
            return env.c.portal.call(env.users.session_store.get_session_by_id, sid).last_activity

        with env.c.websocket_connect(env.shell_url(alice)) as ws:
            _ready(ws)
            before = last_activity()
            time.sleep(0.25)
            ws.send_text(json.dumps({"type": "stdin", "data": "\n"}))
            deadline = time.monotonic() + 3
            while last_activity() == before and time.monotonic() < deadline:
                time.sleep(0.05)
            assert last_activity() > before

    def test_server_shutdown_closes_1001(self, make_env):
        env = make_env()
        with env.c.websocket_connect(env.shell_url(env.root)) as ws:
            _ready(ws)
            env.c.portal.call(env.result.shell_session_manager.close_all)
            assert _until_closed(ws).code == 1001
        meta = env.audit("shell_closed")[-1].metadata
        assert meta["reason"] == "server shutdown" and "exitCode" not in meta


class TestSessionEndsWhileOpening:
    """A logout/revoke between ticket consumption and shell registration
    fires its change event before the shell is registered: the shell must
    still be closed right away, not at the next periodic re-check."""

    @staticmethod
    def _resolve_then(monkeypatch, action):
        from qodalis_cli_users import UsersShellAccess

        original = UsersShellAccess.resolve

        async def resolve(self, websocket):
            principal = await original(self, websocket)
            await action(principal)
            return principal

        monkeypatch.setattr(UsersShellAccess, "resolve", resolve)

    def test_session_ended_before_registration_closes_4401(self, make_env, monkeypatch):
        env = make_env()  # default 30 s periodic re-check
        alice = env.user("alice", ["shell:execute"])
        url = env.shell_url(alice)

        async def logout(principal):
            await env.users.session_store.delete_session_by_id(principal.session_id)

        self._resolve_then(monkeypatch, logout)
        start = time.monotonic()
        with env.c.websocket_connect(url) as ws:
            messages = []
            while True:
                try:
                    messages.append(json.loads(ws.receive_text()))
                except WebSocketDisconnect as disconnect:
                    exc = disconnect
                    break
        assert (exc.code, exc.reason) == (4401, "session ended")
        assert time.monotonic() - start < 5
        assert not any(m["type"] == "ready" for m in messages)
        assert env.result.shell_session_manager.active_sessions == 0

    def test_permission_removed_before_registration_closes_4401(self, make_env, monkeypatch):
        env = make_env()
        alice = env.user("alice", ["shell:execute"])
        url = env.shell_url(alice)

        async def remove_permission(principal):
            await env.users.group_store.update_group("alice-g", permissions=["commands:execute"])

        self._resolve_then(monkeypatch, remove_permission)
        exc = _rejected(env.c, url)
        assert (exc.code, exc.reason) == (4401, "session ended")
        assert env.result.shell_session_manager.active_sessions == 0


class TestAdapterMode:
    """Shells of users resolved by an external auth adapter (not in the user
    store) are re-validated by calling the adapter again with the shell's
    WebSocket request."""

    ORIGIN = "http://app.test"

    @pytest.fixture()
    def adapter_env(self):
        from qodalis_cli_users import ExternalAuthResult, User

        state: dict[str, Any] = {"groups": ["shellers"], "mode": "ok"}

        def adapter(request):
            if state["mode"] == "raise":
                raise RuntimeError("identity provider down")
            name = request.headers.get("x-user")
            if not name or state["mode"] == "none":
                return None
            return ExternalAuthResult(user=User(
                id=f"ext-{name}", name=name, email=f"{name}@x", groups=list(state["groups"]),
            ))

        clients = []

        def factory(**shell: Any):
            shell.setdefault("shell", "/bin/sh")
            result = create_cli_server(CliServerOptions(
                enable_shell=True, shell=CliShellOptions(**shell), allowed_origins=[self.ORIGIN],
            ))
            users = CliUsersBuilder().set_auth_adapter(adapter).build()
            result.mount_plugin(users)
            client = TestClient(result.app)
            client.__enter__()
            clients.append(client)
            client.portal.call(users.group_store.create_group, "shellers", "", ["shell:execute"])
            return client, users

        yield factory, state
        for client in clients:
            client.__exit__(None, None, None)

    def _connect(self, client: TestClient):
        headers = {"x-user": "ext", "origin": self.ORIGIN}
        res = client.post(BASE + "/sessions/ws-ticket", headers=headers)
        assert res.status_code == 201, res.text
        return client.websocket_connect(f"{SHELL}?ticket={res.json()['ticket']}", headers=headers)

    @staticmethod
    def _echo(ws, word: str) -> None:
        ws.send_text(json.dumps({"type": "stdin", "data": f"echo {word}\n"}))
        out = ""
        while word not in out.replace(f"echo {word}", ""):
            out += json.loads(ws.receive_text()).get("data", "")

    def test_adapter_shell_survives_periodic_rechecks(self, adapter_env):
        factory, _ = adapter_env
        client, _ = factory(session_check_interval=0.2)
        with self._connect(client) as ws:
            _ready(ws)
            time.sleep(0.8)  # several re-checks
            self._echo(ws, "still-open")

    @pytest.mark.parametrize("mode", ["none", "raise"])
    def test_adapter_rejecting_closes_session_ended(self, adapter_env, mode):
        factory, state = adapter_env
        client, users = factory(session_check_interval=0.2)
        with self._connect(client) as ws:
            _ready(ws)
            state["mode"] = mode
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")
        closed = client.portal.call(users.audit_store.get_entries, AuditQueryOptions(event="shell_closed"))
        assert closed[-1].metadata["reason"] == "session ended"

    def test_adapter_without_shell_permission_closes(self, adapter_env):
        factory, state = adapter_env
        client, users = factory(session_check_interval=0.2)
        with self._connect(client) as ws:
            _ready(ws)
            state["groups"] = ["users"]
            exc = _until_closed(ws)
            assert (exc.code, exc.reason) == (4401, "session ended")
        closed = client.portal.call(users.audit_store.get_entries, AuditQueryOptions(event="shell_closed"))
        assert closed[-1].metadata["reason"] == "permission removed"
