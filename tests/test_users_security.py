"""Security contract (Appendix B) tests for the users plugin + server."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import (
    CliEchoCommandProcessor,
    CliServerOptions,
    InMemoryFileStorageProvider,
    create_cli_server,
)
from qodalis_cli.abstractions import CliCommandProcessor
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_jobs import CliJobsBuilder
from qodalis_cli_users import (
    AuditEntry,
    CliUsersBuilder,
    ExternalAuthResult,
    InMemoryAuditStore,
    User,
)
from qodalis_cli.security import is_same_origin as core_is_same_origin
from qodalis_cli_users._http import is_same_origin as users_is_same_origin
from qodalis_cli_users.server_protection import http_permission, ws_permission
from qodalis_cli_users.services import password_hasher
from qodalis_cli_users.services.auth_service import hash_token

BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
ALLOWED = "http://allowed.test"


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


class _Sub(CliCommandProcessor):
    def __init__(self, name: str, perms: list[str] | None = None, subs: list | None = None) -> None:
        self._name, self._perms, self._subs = name, perms, subs

    @property
    def command(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def required_permissions(self) -> list[str] | None:
        return self._perms

    @property
    def processors(self):
        return self._subs

    async def handle_async(self, command, cancellation_event=None) -> str:
        return f"ran {self._name}"


def _h(token: str | None = None, **extra: str) -> dict[str, str]:
    headers = dict(extra)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def assert_error(res, status: int, contains: str | None = None) -> None:
    assert res.status_code == status, res.text
    body = res.json()
    assert "error" in body, body
    if contains:
        assert contains in body["error"], body


class Server:
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

    def user(self, name: str, perms: list[str] | None = None, *, in_users: bool = True,
             password: str = "pass1234") -> str:
        admin = self.root
        groups = []
        if perms is not None:
            g = f"{name}-g"
            res = self.c.post(BASE + "/groups", headers=_h(admin), json={"name": g, "permissions": perms})
            assert res.status_code == 201, res.text
            groups.append(g)
        if in_users:
            groups.append("users")
        res = self.c.post(BASE + "/users", headers=_h(admin), json={
            "name": name, "email": f"{name}@example.com", "password": password, "groups": groups,
        })
        assert res.status_code == 201, res.text
        return self.login(name, password)

    def ticket(self, token: str) -> str:
        res = self.c.post(BASE + "/sessions/ws-ticket", headers=_h(token))
        assert res.status_code == 201, res.text
        return res.json()["ticket"]


def _build(users_builder: CliUsersBuilder | None = None, **opts: Any):
    def configure(builder):
        builder.add_processor(CliEchoCommandProcessor())
        builder.add_processor(DeployProcessor())
        builder.set_file_storage_provider(InMemoryFileStorageProvider())

    result = create_cli_server(CliServerOptions(configure=configure, **opts))
    jobs = CliJobsBuilder().build()
    result.mount_plugin(jobs)
    users = None
    if users_builder is not None:
        users = users_builder.build()
        result.mount_plugin(users)
    return result, users


@pytest.fixture()
def srv():
    result, users = _build(
        CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000),
        allowed_origins=[ALLOWED],
    )
    with TestClient(result.app) as client:
        yield Server(client, users, result)


# =========================================================================
# B1 — whole-server protection
# =========================================================================


class TestServerProtection:
    @pytest.mark.parametrize("method,path,body", [
        ("GET", "/api/qcli/commands", None),
        ("GET", "/api/v1/qcli/commands", None),
        ("POST", "/api/qcli/execute", {"command": "echo", "value": "hi"}),
        ("POST", "/api/v1/qcli/execute/stream", {"command": "echo", "value": "hi"}),
        ("GET", "/api/qcli/fs/ls?path=/", None),
        ("POST", "/api/qcli/fs/mkdir", {"path": "/x"}),
        ("GET", "/api/v1/qcli/jobs", None),
    ])
    def test_anonymous_denied(self, srv: Server, method, path, body):
        res = srv.c.request(method, path, json=body)
        assert_error(res, 401)

    @pytest.mark.parametrize("path", [
        "/api/qcli/versions", "/api/v1/qcli/version", "/api/v1/qcli/capabilities", "/api/qcli/capabilities",
    ])
    def test_version_discovery_public(self, srv: Server, path):
        assert srv.c.get(path).status_code == 200

    def test_invalid_token_denied(self, srv: Server):
        assert_error(srv.c.get("/api/qcli/commands", headers=_h("nope")), 401)

    def test_regular_user_can_execute(self, srv: Server):
        token = srv.user("alice")
        assert srv.c.get("/api/qcli/commands", headers=_h(token)).status_code == 200
        res = srv.c.post("/api/qcli/execute", headers=_h(token), json={"command": "echo", "value": "hi"})
        assert res.status_code == 200, res.text

    def test_commands_execute_required(self, srv: Server):
        token = srv.user("bob", [], in_users=False)
        assert srv.c.get("/api/qcli/commands", headers=_h(token)).status_code == 200
        res = srv.c.post("/api/qcli/execute", headers=_h(token), json={"command": "echo"})
        assert_error(res, 403, "commands:execute")

    def test_processor_required_permissions(self, srv: Server):
        plain = srv.user("alice")
        for path in ("/api/qcli/execute", "/api/qcli/execute/stream"):
            res = srv.c.post(path, headers=_h(plain), json={"command": "deploy"})
            assert_error(res, 403, "deploy:run")
        deployer = srv.user("dev", ["deploy:run"])
        res = srv.c.post("/api/qcli/execute", headers=_h(deployer), json={"command": "deploy"})
        assert res.status_code == 200 and "deployed" in res.text
        cmds = srv.c.get("/api/qcli/commands", headers=_h(deployer)).json()
        assert next(c for c in cmds if c["command"] == "deploy")["requiredPermissions"] == ["deploy:run"]

    def test_intermediate_sub_processor_permissions_enforced(self):
        def configure(builder):
            builder.add_processor(_Sub("release", None, [_Sub("prod", ["release:prod"], [_Sub("run")])]))

        result = create_cli_server(CliServerOptions(configure=configure))
        users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
        result.mount_plugin(users)
        with TestClient(result.app) as client:
            s = Server(client, users, result)
            plain = s.user("alice")
            body = {"command": "release", "chainCommands": ["prod", "run"]}
            for path in ("/api/qcli/execute", "/api/qcli/execute/stream"):
                assert_error(s.c.post(path, headers=_h(plain), json=body), 403, "release:prod")
            releaser = s.user("rel", ["release:prod"])
            res = s.c.post("/api/qcli/execute", headers=_h(releaser), json=body)
            assert res.status_code == 200 and "ran run" in res.text

    @pytest.mark.parametrize("base_path", ["/cli", "/api/custom"])
    def test_custom_base_path_is_protected(self, base_path):
        result, users = _build(
            CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000),
            base_path=base_path,
        )
        with TestClient(result.app) as client:
            s = Server(client, users, result)
            assert_error(s.c.get(base_path + "/commands"), 401)
            assert_error(s.c.post(base_path + "/execute", json={"command": "echo", "value": "hi"}), 401)
            assert_error(s.c.post(base_path + "/execute/stream", json={"command": "echo"}), 401)
            assert s.c.get(base_path + "/version").status_code == 200
            token = s.user("bob", [], in_users=False)
            assert s.c.get(base_path + "/commands", headers=_h(token)).status_code == 200
            assert_error(s.c.post(base_path + "/execute", headers=_h(token), json={"command": "echo"}),
                         403, "commands:execute")
            alice = s.user("alice")
            res = s.c.post(base_path + "/execute", headers=_h(alice), json={"command": "echo", "value": "hi"})
            assert res.status_code == 200, res.text

    def test_files_permissions(self, srv: Server):
        plain = srv.user("alice")
        assert_error(srv.c.get("/api/qcli/fs/ls?path=/", headers=_h(plain)), 403, "files:read")
        reader = srv.user("reader", ["files:read"])
        assert srv.c.get("/api/qcli/fs/ls?path=/", headers=_h(reader)).status_code != 403
        assert_error(srv.c.post("/api/qcli/fs/mkdir", headers=_h(reader), json={"path": "/x"}), 403, "files:write")
        writer = srv.user("writer", ["files:write"])
        assert srv.c.post("/api/qcli/fs/mkdir", headers=_h(writer), json={"path": "/x"}).status_code != 403

    def test_jobs_permissions(self, srv: Server):
        plain = srv.user("alice")
        assert_error(srv.c.get("/api/v1/qcli/jobs", headers=_h(plain)), 403, "jobs:read")
        reader = srv.user("reader", ["jobs:read"])
        assert srv.c.get("/api/v1/qcli/jobs", headers=_h(reader)).status_code == 200
        assert_error(srv.c.post("/api/v1/qcli/jobs/x/trigger", headers=_h(reader)), 403, "jobs:manage")

    def test_permission_rules(self):
        assert http_permission("/api/qcli/data-explorer/execute", "POST") == "data:query"
        assert http_permission("/api/qcli/data-explorer/sources", "GET") == "data:query"
        assert http_permission("/api/qcli/fs/download", "GET") == "files:read"
        assert http_permission("/api/qcli/fs/rm", "DELETE") == "files:write"
        assert http_permission("/api/qcli/fs/upload", "POST") == "files:write"
        assert http_permission("/api/qcli/commands", "GET") is None
        assert ws_permission("/ws/qcli/logs") == "logs:read"
        assert ws_permission("/ws/v1/qcli/shell") == "shell:execute"
        assert ws_permission("/ws/v1/qcli/events") is None

    def test_admin_star_covers_everything(self, srv: Server):
        root = srv.root
        assert srv.c.get("/api/v1/qcli/jobs", headers=_h(root)).status_code == 200
        res = srv.c.post("/api/qcli/execute", headers=_h(root), json={"command": "deploy"})
        assert res.status_code == 200

    def test_protect_server_can_be_disabled(self):
        result, _ = _build(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_protect_server(False))
        with TestClient(result.app) as c:
            assert c.post("/api/qcli/execute", json={"command": "echo"}).status_code == 200

    def test_host_guards(self):
        users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).build()
        app = FastAPI()
        app.include_router(users.router, prefix=users.prefix)

        @app.get("/host/me")
        async def me(ctx=Depends(users.require_auth)):
            return {"name": ctx.user.name}

        @app.get("/host/deploy")
        async def deploy(ctx=Depends(users.require_permission("deploy:run"))):
            return {"ok": True}

        c = TestClient(app)
        assert c.get("/host/me").status_code == 401
        token = c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS}).json()["token"]
        assert c.get("/host/me", headers=_h(token)).json() == {"name": "root"}
        assert c.get("/host/deploy", headers=_h(token)).status_code == 200
        c.post(BASE + "/users", headers=_h(token), json={"name": "al", "email": "al@x", "password": "pass1234"})
        al = c.post(BASE + "/sessions", json={"username": "al", "password": "pass1234"}).json()["token"]
        assert c.get("/host/deploy", headers=_h(al)).status_code == 403


class TestWithoutUsersPlugin:
    def test_server_unchanged(self):
        result, _ = _build()
        with TestClient(result.app) as c:
            assert c.post("/api/qcli/execute", json={"command": "echo", "value": "x"}).status_code == 200
            assert c.get("/api/qcli/commands").status_code == 200
            assert c.get("/api/qcli/fs/ls?path=/").status_code != 401
            with c.websocket_connect("/ws/qcli/events") as ws:
                assert json.loads(ws.receive_text())["type"] == "connected"

    def test_cors_disabled_by_default(self):
        result, _ = _build()
        with TestClient(result.app) as c:
            res = c.get("/api/qcli/versions", headers={"Origin": "http://evil.test"})
            assert "access-control-allow-origin" not in res.headers

    def test_cors_allowed_origins(self):
        result, _ = _build(allowed_origins=[ALLOWED, "http://127.0.0.1:*"])
        with TestClient(result.app) as c:
            ok = c.get("/api/qcli/versions", headers={"Origin": ALLOWED})
            assert ok.headers["access-control-allow-origin"] == ALLOWED
            assert ok.headers["access-control-allow-credentials"] == "true"
            glob = c.get("/api/qcli/versions", headers={"Origin": "http://127.0.0.1:4300"})
            assert glob.headers["access-control-allow-origin"] == "http://127.0.0.1:4300"
            bad = c.get("/api/qcli/versions", headers={"Origin": "http://evil.test"})
            assert "access-control-allow-origin" not in bad.headers

    def test_cors_explicit_wildcard_has_no_credentials(self):
        result, _ = _build(cors=True)
        with TestClient(result.app) as c:
            res = c.get("/api/qcli/versions", headers={"Origin": "http://any.test"})
            assert res.headers["access-control-allow-origin"] == "*"
            assert "access-control-allow-credentials" not in res.headers

    def test_shell_disabled_by_default(self):
        result, _ = _build()
        with TestClient(result.app) as c:
            assert c.get("/api/qcli/capabilities").json()["shell"] is False
            with pytest.raises(WebSocketDisconnect) as exc:
                with c.websocket_connect("/ws/qcli/shell") as ws:
                    ws.receive_text()
            assert exc.value.code == 4404

    def test_ws_origin_check_without_users(self):
        result, _ = _build(allowed_origins=[ALLOWED])
        with TestClient(result.app) as c:
            with c.websocket_connect("/ws/qcli/events", headers={"origin": "http://evil.test"}) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
                assert exc.value.code == 4403
            with c.websocket_connect("/ws/qcli/events", headers={"origin": ALLOWED}) as ws:
                assert json.loads(ws.receive_text())["type"] == "connected"
            # Non-browser clients send no Origin at all: accepted
            with c.websocket_connect("/ws/qcli/events") as ws:
                assert json.loads(ws.receive_text())["type"] == "connected"


# =========================================================================
# B2 — WebSockets
# =========================================================================


def _ws_closed_with(client: TestClient, url: str, code: int, origin: str = ALLOWED) -> None:
    with client.websocket_connect(url, headers={"origin": origin}) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == code


class TestWebSockets:
    def test_ticket_shape(self, srv: Server):
        res = srv.c.post(BASE + "/sessions/ws-ticket", headers=_h(srv.root))
        assert res.status_code == 201
        body = res.json()
        assert set(body) == {"ticket", "expiresIn"}
        assert re.fullmatch(r"[0-9a-f]{64}", body["ticket"])
        assert body["expiresIn"] == 30

    def test_ticket_requires_auth(self, srv: Server):
        assert_error(srv.c.post(BASE + "/sessions/ws-ticket"), 401)

    def test_events_with_ticket_single_use(self, srv: Server):
        ticket = srv.ticket(srv.root)
        with srv.c.websocket_connect(f"/ws/qcli/events?ticket={ticket}", headers={"origin": ALLOWED}) as ws:
            assert json.loads(ws.receive_text())["type"] == "connected"
        _ws_closed_with(srv.c, f"/ws/qcli/events?ticket={ticket}", 4401)

    def test_missing_or_bad_ticket(self, srv: Server):
        _ws_closed_with(srv.c, "/ws/qcli/events", 4401)
        _ws_closed_with(srv.c, "/ws/qcli/events?ticket=deadbeef", 4401)

    def test_bearer_in_query_not_accepted(self, srv: Server):
        token = srv.root
        _ws_closed_with(srv.c, f"/ws/qcli/events?token={token}", 4401)
        _ws_closed_with(srv.c, f"/ws/qcli/events?_header_Authorization=Bearer%20{token}", 4401)

    def test_ticket_expires(self, srv: Server):
        srv.users.auth_service.options.ticket_ttl_ms = 1
        ticket = srv.ticket(srv.root)
        import time
        time.sleep(0.01)
        _ws_closed_with(srv.c, f"/ws/qcli/events?ticket={ticket}", 4401)

    def test_ticket_dies_with_session(self, srv: Server):
        token = srv.root
        ticket = srv.ticket(token)
        srv.c.delete(BASE + "/sessions/current", headers=_h(token))
        _ws_closed_with(srv.c, f"/ws/qcli/events?ticket={ticket}", 4401)

    def test_origin_checked(self, srv: Server):
        ticket = srv.ticket(srv.root)
        _ws_closed_with(srv.c, f"/ws/qcli/events?ticket={ticket}", 4403, origin="http://evil.test")

    def test_logs_requires_logs_read(self, srv: Server):
        plain = srv.user("alice")
        _ws_closed_with(srv.c, f"/ws/qcli/logs?ticket={srv.ticket(plain)}", 4403)
        reader = srv.user("ops", ["logs:read"])
        with srv.c.websocket_connect(f"/ws/qcli/logs?ticket={srv.ticket(reader)}", headers={"origin": ALLOWED}) as ws:
            assert json.loads(ws.receive_text())["type"] == "connected"

    def test_shell_one_shot_streams_output_and_exit(self):
        result, users = _build(
            CliUsersBuilder().set_default_admin_password(ROOT_PASS), enable_shell=True,
        )
        with TestClient(result.app) as c:
            s = Server(c, users, result)
            url = f"/ws/qcli/shell?cols=80&rows=24&cmd=echo%20one-shot-ok&ticket={s.ticket(s.root)}"
            stdout, exit_code = "", None
            with c.websocket_connect(url) as ws:
                for _ in range(50):
                    msg = json.loads(ws.receive_text())
                    if msg["type"] == "stdout":
                        stdout += msg["data"]
                    elif msg["type"] == "exit":
                        exit_code = msg["code"]
                        break
            assert "one-shot-ok" in stdout
            assert exit_code == 0

    def test_shell_requires_shell_execute(self):
        result, users = _build(
            CliUsersBuilder().set_default_admin_password(ROOT_PASS), enable_shell=True,
        )
        with TestClient(result.app) as c:
            s = Server(c, users, result)
            assert c.get("/api/qcli/capabilities").json()["shell"] is True
            plain = s.user("alice")
            with c.websocket_connect(f"/ws/qcli/shell?ticket={s.ticket(plain)}") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
                assert exc.value.code == 4403
            with c.websocket_connect("/ws/qcli/shell?cmd=id") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
                assert exc.value.code == 4401

    def test_session_kicked_only_to_that_user(self, srv: Server):
        alice = srv.user("alice")
        bob = srv.user("bob")
        root = srv.root
        alice_sid = srv.c.get(BASE + "/sessions/current", headers=_h(alice)).json()["id"]
        bob_sid = srv.c.get(BASE + "/sessions/current", headers=_h(bob)).json()["id"]
        with srv.c.websocket_connect(f"/ws/qcli/events?ticket={srv.ticket(alice)}", headers={"origin": ALLOWED}) as wa, \
                srv.c.websocket_connect(f"/ws/qcli/events?ticket={srv.ticket(bob)}", headers={"origin": ALLOWED}) as wb:
            assert json.loads(wa.receive_text())["type"] == "connected"
            assert json.loads(wb.receive_text())["type"] == "connected"
            assert srv.c.delete(f"{BASE}/sessions/{alice_sid}", headers=_h(root)).status_code == 204
            msg = json.loads(wa.receive_text())
            assert msg == {"type": "session-kicked", "data": {
                "sessionId": alice_sid, "userId": msg["data"]["userId"], "reason": "revoked"}}
            assert srv.c.delete(f"{BASE}/sessions/{bob_sid}", headers=_h(root)).status_code == 204
            # Bob's first event is his own kick: he never saw Alice's.
            assert json.loads(wb.receive_text())["data"]["sessionId"] == bob_sid


# =========================================================================
# B3 — no privilege escalation
# =========================================================================


class TestEscalation:
    @pytest.fixture()
    def mgr(self, srv: Server) -> str:
        return srv.user("mgr", ["users:*", "groups:*", "sessions:*"])

    def test_cannot_grant_unheld_permissions_to_group(self, srv: Server, mgr: str):
        res = srv.c.post(BASE + "/groups", headers=_h(mgr), json={"name": "g1", "permissions": ["files:write"]})
        assert_error(res, 403, "cannot grant permissions you do not hold: files:write")
        assert_error(srv.c.post(BASE + "/groups", headers=_h(mgr), json={"name": "g2", "permissions": ["*"]}), 403)
        assert srv.c.post(BASE + "/groups", headers=_h(mgr), json={"name": "g3", "permissions": ["users:read"]}).status_code == 201
        assert_error(srv.c.put(BASE + "/groups/users", headers=_h(mgr), json={"permissions": ["*"]}), 403)
        assert_error(srv.c.put(BASE + "/groups/g3", headers=_h(mgr), json={"permissions": ["audit:*"]}), 403)

    def test_cannot_join_more_privileged_group(self, srv: Server, mgr: str):
        assert_error(srv.c.post(BASE + "/groups/admin/members/mgr", headers=_h(mgr)), 403)
        assert_error(srv.c.put(BASE + "/users/mgr", headers=_h(mgr), json={"groups": ["admin", "mgr-g"]}), 403)
        res = srv.c.post(BASE + "/users", headers=_h(mgr), json={
            "name": "evil", "email": "evil@x", "password": "pass1234", "groups": ["admin"]})
        assert_error(res, 403, "cannot grant permissions you do not hold")

    def test_cannot_act_on_more_privileged_user(self, srv: Server, mgr: str):
        root_sid = srv.c.get(BASE + "/sessions/current", headers=_h(srv.root)).json()["id"]
        for res in (
            srv.c.put(BASE + "/users/root", headers=_h(mgr), json={"disabled": True}),
            srv.c.put(BASE + "/users/root", headers=_h(mgr), json={"name": "rooty"}),
            srv.c.put(BASE + "/users/root/password", headers=_h(mgr), json={"password": "hacked123"}),
            srv.c.delete(BASE + "/users/root", headers=_h(mgr)),
            srv.c.delete(f"{BASE}/sessions/{root_sid}", headers=_h(mgr)),
            srv.c.delete(BASE + "/groups/admin/members/root", headers=_h(mgr)),
        ):
            assert_error(res, 403, "insufficient privileges for this user")
        assert srv.c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS}).status_code == 201

    def test_can_act_on_covered_user(self, srv: Server, mgr: str):
        srv.user("plain", [], in_users=False)
        assert srv.c.put(BASE + "/users/plain", headers=_h(mgr), json={"disabled": True}).status_code == 200
        assert srv.c.put(BASE + "/users/plain/password", headers=_h(mgr), json={"password": "newpass99"}).status_code == 204
        assert srv.c.delete(BASE + "/users/plain", headers=_h(mgr)).status_code == 204

    def test_users_group_needs_commands_execute(self, srv: Server):
        # A manager without commands:execute cannot add people to 'users'.
        weak = srv.user("weak", ["users:*", "groups:*"], in_users=False)
        srv.user("target", [], in_users=False)
        assert_error(srv.c.post(BASE + "/groups/users/members/target", headers=_h(weak)), 403)


# =========================================================================
# B4 — default credentials, mustChangePassword, policy
# =========================================================================


class TestDefaultCredentials:
    def test_generated_root_password(self, caplog):
        result, users = _build(CliUsersBuilder())
        with caplog.at_level(logging.WARNING):
            with TestClient(result.app) as c:
                c.post(BASE + "/sessions", json={"username": "nobody", "password": "x"})  # seeds
                c.post(BASE + "/sessions", json={"username": "nobody", "password": "x"})
                records = [r for r in caplog.records if "Generated a one-time password" in r.getMessage()]
                assert len(records) == 1
                assert getattr(records[0], "qcli_sensitive", False) is True
                password = re.search(r"'root': (\S+)", records[0].getMessage()).group(1)
                assert len(password) == 16
                res = c.post(BASE + "/sessions", json={"username": "root", "password": password})
                assert res.status_code == 201
                body = res.json()
                assert body["session"]["user"]["mustChangePassword"] is True
                token = body["token"]
                # Gated
                assert_error(c.get("/api/qcli/commands", headers=_h(token)), 403, "password change required")
                assert_error(c.get(BASE + "/users", headers=_h(token)), 403, "password change required")
                assert_error(c.post(BASE + "/sessions/ws-ticket", headers=_h(token)), 403, "password change required")
                # Allowed
                assert c.get(BASE + "/sessions/current", headers=_h(token)).status_code == 200
                assert_error(c.put(BASE + "/users/root/password", headers=_h(token), json={"password": "newroot123"}), 400)
                res = c.put(BASE + "/users/root/password", headers=_h(token),
                            json={"currentPassword": password, "password": "newroot123"})
                assert res.status_code == 204
                current = c.get(BASE + "/sessions/current", headers=_h(token)).json()
                assert current["user"]["mustChangePassword"] is False
                assert c.get("/api/qcli/commands", headers=_h(token)).status_code == 200
                assert c.delete(BASE + "/sessions/current", headers=_h(token)).status_code == 204

    def test_generated_password_not_streamed_to_log_socket(self):
        from qodalis_cli.services import CliLogSocketManager, WebSocketLogHandler

        sent: list[tuple] = []
        manager = CliLogSocketManager()
        manager.broadcast_log = lambda *a: sent.append(a)  # type: ignore[assignment]
        handler = WebSocketLogHandler(manager)
        record = logging.LogRecord("x", logging.WARNING, "", 0, "pw %s", ("secret",), None)
        record.qcli_sensitive = True
        handler.emit(record)
        assert sent == []
        handler.emit(logging.LogRecord("x", logging.INFO, "", 0, "Authorization: Bearer abc123", (), None))
        assert "abc123" not in sent[0][1]

    def test_weak_explicit_password_warns(self, caplog):
        result, users = _build(CliUsersBuilder().set_default_admin_password("admin"))
        with caplog.at_level(logging.WARNING):
            with TestClient(result.app) as c:
                body = c.post(BASE + "/sessions", json={"username": "root", "password": "admin"}).json()
                assert body["session"]["user"]["mustChangePassword"] is False
                assert any("well-known default password" in r.getMessage() for r in caplog.records)

    def test_admin_reset_sets_must_change(self, srv: Server):
        root = srv.root
        srv.user("alice")
        srv.c.put(BASE + "/users/alice/password", headers=_h(root), json={"password": "temp12345"})
        assert srv.c.get(BASE + "/users/alice", headers=_h(root)).json()["mustChangePassword"] is True
        alice = srv.login("alice", "temp12345")
        assert_error(srv.c.get("/api/qcli/commands", headers=_h(alice)), 403, "password change required")
        assert_error(srv.c.put(BASE + "/users/root/password", headers=_h(alice), json={"password": "x" * 9}), 403)
        srv.c.put(BASE + "/users/alice/password", headers=_h(root), json={"password": "final12345", "temporary": False})
        assert srv.c.get(BASE + "/users/alice", headers=_h(root)).json()["mustChangePassword"] is False

    def test_password_policy(self, srv: Server):
        root = srv.root
        assert_error(srv.c.post(BASE + "/users", headers=_h(root), json={
            "name": "a", "email": "a@x", "password": "short12"}), 400, "at least 8")
        assert_error(srv.c.post(BASE + "/users", headers=_h(root), json={
            "name": "a", "email": "a@x", "password": "x" * 1025}), 400, "at most 1024")
        assert srv.c.post(BASE + "/users", headers=_h(root), json={
            "name": "a", "email": "a@x", "password": "x" * 1024}).status_code == 201


# =========================================================================
# B5 — brute force, enumeration, DoS
# =========================================================================


def _users_app(builder: CliUsersBuilder) -> tuple[FastAPI, Any]:
    plugin = builder.build()
    app = FastAPI()
    app.include_router(plugin.router, prefix=plugin.prefix)
    return app, plugin


class TestBruteForce:
    async def test_lockout_is_atomic_under_concurrency(self):
        app, plugin = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            results = await asyncio.gather(*[
                c.post(BASE + "/sessions", json={"username": "root", "password": f"wrong{i}"})
                for i in range(20)
            ])
            codes = [r.status_code for r in results]
            assert codes.count(401) == 5, codes
            assert codes.count(429) == 15, codes
            res = await c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS})
            assert res.status_code == 429

    def test_unknown_users_locked_like_known(self):
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000))
        c = TestClient(app)
        for _ in range(5):
            assert_error(c.post(BASE + "/sessions", json={"username": "ghost", "password": "x"}), 401, "invalid credentials")
        assert_error(c.post(BASE + "/sessions", json={"username": "ghost", "password": "x"}), 429, "account locked, try again later")

    async def test_unknown_user_runs_pbkdf2(self, monkeypatch):
        calls: list[str] = []
        original = password_hasher.verify_password

        def spy(password, encoded, legacy_salt=""):
            calls.append(threading.current_thread().name)
            return original(password, encoded, legacy_salt)

        monkeypatch.setattr(password_hasher, "verify_password", spy)
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(BASE + "/sessions", json={"username": "nobody", "password": "x"})
        assert calls and all(name.startswith("qcli-pbkdf2") for name in calls)
        assert password_hasher._executor._max_workers == 4

    def test_per_ip_rate_limit(self):
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(3))
        c = TestClient(app)
        for i in range(3):
            c.post(BASE + "/sessions", json={"username": f"u{i}", "password": "x"},
                   headers={"X-Forwarded-For": f"10.0.0.{i}"})
        # X-Forwarded-For is not trusted by default
        assert_error(c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS},
                            headers={"X-Forwarded-For": "10.9.9.9"}), 429, "too many requests")

    def test_per_ip_rate_limit_trust_proxy(self):
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS)
                            .set_rate_limit(2).set_trust_proxy())
        c = TestClient(app)
        for _ in range(2):
            c.post(BASE + "/sessions", json={"username": "x", "password": "x"}, headers={"X-Forwarded-For": "10.0.0.1"})
        assert_error(c.post(BASE + "/sessions", json={"username": "x", "password": "x"},
                            headers={"X-Forwarded-For": "10.0.0.1"}), 429)
        res = c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS},
                     headers={"X-Forwarded-For": "10.0.0.2"})
        assert res.status_code == 201

    def test_session_remote_address_uses_forwarded_for_with_trust_proxy(self):
        app, plugin = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_trust_proxy())
        c = TestClient(app)
        res = c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS},
                     headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
        assert res.status_code == 201
        token = res.json()["token"]
        sessions = c.get(BASE + "/sessions", headers=_h(token)).json()
        assert sessions[0]["metadata"]["remoteAddress"] == "203.0.113.7"
        logins = c.get(BASE + "/audit?event=login", headers=_h(token)).json()
        assert logins[0]["metadata"]["remoteAddress"] == "203.0.113.7"

    def test_rate_limit_covers_verify_and_self_password_change(self):
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(3))
        c = TestClient(app)
        token = c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS}).json()["token"]
        c.post(BASE + "/users/root/verify-password", headers=_h(token), json={"password": ROOT_PASS})
        c.put(BASE + "/users/root/password", headers=_h(token), json={"password": "another123"})
        assert_error(c.post(BASE + "/users/root/verify-password", headers=_h(token), json={"password": "x"}), 429)

    def test_self_password_change_lockout_and_audit(self, srv: Server):
        alice = srv.user("alice")
        for _ in range(5):
            res = srv.c.put(BASE + "/users/alice/password", headers=_h(alice),
                            json={"currentPassword": "wrong", "password": "newpass99"})
            assert_error(res, 403, "current password is incorrect")
        assert_error(srv.c.put(BASE + "/users/alice/password", headers=_h(alice),
                               json={"currentPassword": "pass1234", "password": "newpass99"}), 429)
        entries = srv.c.get(BASE + "/audit?event=login_failed&username=alice", headers=_h(srv.root)).json()
        assert len(entries) >= 5

    def test_admin_reset_clears_lockout(self, srv: Server):
        srv.user("alice")
        for _ in range(5):
            srv.c.post(BASE + "/sessions", json={"username": "alice", "password": "wrong"})
        assert srv.c.post(BASE + "/sessions", json={"username": "alice", "password": "pass1234"}).status_code == 429
        srv.c.put(BASE + "/users/alice/password", headers=_h(srv.root), json={"password": "reset1234", "temporary": False})
        assert srv.c.post(BASE + "/sessions", json={"username": "alice", "password": "reset1234"}).status_code == 201

    def test_absolute_session_lifetime(self):
        import time
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS)
                            .set_max_session_lifetime(300).set_session_timeout(60_000))
        c = TestClient(app)
        body = c.post(BASE + "/sessions", json={"username": "root", "password": ROOT_PASS}).json()
        session = body["session"]
        assert session["expiresAt"] == session["loginTime"] + 300
        for _ in range(3):
            time.sleep(0.05)
            assert c.get(BASE + "/sessions/current", headers=_h(body["token"])).status_code == 200
        time.sleep(0.3)
        assert_error(c.get(BASE + "/sessions/current", headers=_h(body["token"])), 401)


# =========================================================================
# B6 — tokens and secrets
# =========================================================================


class TestTokens:
    def test_store_keeps_only_token_hash(self, srv: Server):
        token = srv.root
        store = srv.users.session_store
        assert hash_token(token) in store._sessions  # type: ignore[attr-defined]
        assert token not in store._sessions  # type: ignore[attr-defined]
        dumped = repr(list(store._sessions.values()))  # type: ignore[attr-defined]
        assert token not in dumped

    def test_tokens_not_in_audit_or_logs(self, srv: Server, caplog):
        with caplog.at_level(logging.DEBUG):
            token = srv.root
            ticket = srv.ticket(token)
            with srv.c.websocket_connect(f"/ws/qcli/events?ticket={ticket}", headers={"origin": ALLOWED}) as ws:
                ws.receive_text()
            audit = json.dumps(srv.c.get(BASE + "/audit", headers=_h(token)).json())
        assert token not in audit and ticket not in audit and ROOT_PASS not in audit
        logs = "\n".join(r.getMessage() for r in caplog.records)
        assert token not in logs and ticket not in logs and ROOT_PASS not in logs


# =========================================================================
# B7 — input validation
# =========================================================================


class TestValidation:
    @pytest.mark.parametrize("name", ["-x", "a b", "a" * 65, "a\x1b[31m", "a/b", "é"])
    def test_bad_user_names(self, srv: Server, name):
        res = srv.c.post(BASE + "/users", headers=_h(srv.root), json={"name": name, "email": "n@x"})
        assert_error(res, 400)

    @pytest.mark.parametrize("name", ["a", "A.b_c-d@e", "x" * 64, "0day"])
    def test_good_user_names(self, srv: Server, name):
        res = srv.c.post(BASE + "/users", headers=_h(srv.root), json={"name": name, "email": "mail@x"})
        assert res.status_code == 201, res.text

    @pytest.mark.parametrize("email", ["nope", "a@", "@b", "a b@c", "a@b c", "a\x00@b", ("a" * 250) + "@b.io"])
    def test_bad_emails(self, srv: Server, email):
        res = srv.c.post(BASE + "/users", headers=_h(srv.root), json={"name": "u", "email": email})
        assert_error(res, 400)

    def test_localhost_email_valid(self, srv: Server):
        res = srv.c.post(BASE + "/users", headers=_h(srv.root), json={"name": "u", "email": "u@localhost"})
        assert res.status_code == 201

    @pytest.mark.parametrize("perm", ["users::x", "a b", "users:*:x:", "x" * 129, ":x", "users:\x1b"])
    def test_bad_permissions(self, srv: Server, perm):
        res = srv.c.post(BASE + "/groups", headers=_h(srv.root), json={"name": "g", "permissions": [perm]})
        assert_error(res, 400)

    def test_permission_limits(self, srv: Server):
        root = srv.root
        perms = [f"p{i}:read" for i in range(101)]
        assert_error(srv.c.post(BASE + "/groups", headers=_h(root), json={"name": "g", "permissions": perms}), 400)
        ok = ["*", "users:*", "Deploy:Run", "a.b-c_d:e"]
        assert srv.c.post(BASE + "/groups", headers=_h(root), json={"name": "g", "permissions": ok}).status_code == 201

    def test_group_name_and_description(self, srv: Server):
        root = srv.root
        assert_error(srv.c.post(BASE + "/groups", headers=_h(root), json={"name": "bad name"}), 400)
        assert_error(srv.c.post(BASE + "/groups", headers=_h(root), json={"name": "g", "description": "x" * 257}), 400)
        assert_error(srv.c.post(BASE + "/groups", headers=_h(root), json={"name": "g", "description": "a\x1b[2J"}), 400)
        assert srv.c.post(BASE + "/groups", headers=_h(root), json={"name": "g", "description": "x" * 256}).status_code == 201

    def test_data_and_body_limits(self, srv: Server):
        root = srv.root
        big = {"k": "x" * (16 * 1024)}
        assert_error(srv.c.post(BASE + "/users", headers=_h(root), json={"name": "u", "email": "u@x", "data": big}), 400)
        huge = "x" * (1024 * 1024 + 10)
        res = srv.c.post(BASE + "/users", headers=_h(root), json={"name": "u", "email": "u@x", "data": {"k": huge}})
        assert_error(res, 400, "too large")

    async def test_chunked_body_limit_stops_reading(self):
        app, _ = _users_app(CliUsersBuilder().set_default_admin_password(ROOT_PASS))
        sent = 0

        async def body():
            nonlocal sent
            for _ in range(800):  # 50 MB, no Content-Length
                sent += 65536
                yield b"x" * 65536

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.post(BASE + "/sessions", content=body(),
                               headers={"content-type": "application/json"})
        assert_error(res, 400, "too large")
        assert sent <= 2 * 1024 * 1024, f"read {sent} bytes before refusing"

    def test_name_email_shared_namespace(self, srv: Server):
        root = srv.root
        srv.c.post(BASE + "/users", headers=_h(root), json={"name": "alice", "email": "alice@corp"})
        assert_error(srv.c.post(BASE + "/users", headers=_h(root), json={"name": "alice@corp", "email": "z@z"}), 409)
        assert_error(srv.c.post(BASE + "/users", headers=_h(root), json={"name": "bob", "email": "ALICE"}), 400)
        srv.c.post(BASE + "/users", headers=_h(root), json={"name": "carol@corp", "email": "carol@x"})
        assert_error(srv.c.post(BASE + "/users", headers=_h(root), json={"name": "dave", "email": "carol@corp"}), 409)
        assert_error(srv.c.put(BASE + "/users/alice", headers=_h(root), json={"name": "carol@x"}), 409)

    def test_audit_sanitizes_attempted_names(self, srv: Server):
        srv.c.post(BASE + "/sessions", json={"username": "\x1b[31mevil" + "y" * 100, "password": "x"})
        entry = srv.c.get(BASE + "/audit?event=login_failed", headers=_h(srv.root)).json()[0]
        assert "\x1b" not in entry["username"]
        assert len(entry["username"]) <= 64
        assert entry["username"].startswith("[31mevil")


# =========================================================================
# B8 — audit
# =========================================================================


class TestAudit:
    def test_user_and_actor_ids(self, srv: Server):
        root = srv.root
        root_id = srv.c.get(BASE + "/sessions/current", headers=_h(root)).json()["user"]["id"]
        user = srv.c.post(BASE + "/users", headers=_h(root), json={"name": "al", "email": "al@x"}).json()
        entry = srv.c.get(BASE + "/audit?event=user_created", headers=_h(root)).json()[0]
        assert entry["userId"] == user["id"]
        assert entry["actorId"] == root_id
        assert entry["actor"] == "root"

    async def test_failed_logins_capped_separately(self):
        store = InMemoryAuditStore()
        for i in range(10):
            await store.log(AuditEntry(event="user_created", username=f"u{i}"))
        for i in range(1500):
            await store.log(AuditEntry(event="login_failed", username=f"x{i}"))
        entries = await store.get_entries()
        assert sum(1 for e in entries if e.event == "user_created") == 10
        assert sum(1 for e in entries if e.event == "login_failed") == 200
        assert entries[0].username == "x1499"


# =========================================================================
# B9 — CSRF (adapter mode)
# =========================================================================


def _adapter(request):
    name = request.headers.get("x-user")
    if not name:
        return None
    return ExternalAuthResult(user=User(id=f"ext-{name}", name=name, email=f"{name}@x"), is_admin=True)


class TestCsrf:
    @pytest.fixture()
    def ext(self):
        result, users = _build(
            CliUsersBuilder().set_auth_adapter(_adapter), allowed_origins=[ALLOWED],
        )
        with TestClient(result.app) as c:
            yield c

    def test_state_change_without_origin_rejected(self, ext: TestClient):
        res = ext.post(BASE + "/groups", headers={"x-user": "boss"}, json={"name": "g1"})
        assert_error(res, 403, "origin not allowed")
        res = ext.post(BASE + "/groups", headers={"x-user": "boss", "origin": "http://evil.test"}, json={"name": "g1"})
        assert_error(res, 403, "origin not allowed")
        res = ext.post("/api/qcli/execute", headers={"x-user": "boss"}, json={"command": "echo"})
        assert_error(res, 403, "origin not allowed")

    def test_allowed_origin_or_reads_pass(self, ext: TestClient):
        res = ext.post(BASE + "/groups", headers={"x-user": "boss", "origin": ALLOWED}, json={"name": "g1"})
        assert res.status_code == 201
        assert ext.get(BASE + "/groups", headers={"x-user": "boss"}).status_code == 200
        res = ext.post("/api/qcli/execute", headers={"x-user": "boss", "origin": ALLOWED}, json={"command": "echo"})
        assert res.status_code == 200

    def test_adapter_ticket_flow(self, ext: TestClient):
        res = ext.post(BASE + "/sessions/ws-ticket", headers={"x-user": "boss", "origin": ALLOWED})
        ticket = res.json()["ticket"]
        with ext.websocket_connect(f"/ws/qcli/events?ticket={ticket}", headers={"origin": ALLOWED}) as ws:
            assert json.loads(ws.receive_text())["type"] == "connected"


# =========================================================================
# B10 — admin dashboard via users
# =========================================================================


class _StubRegistry:
    processors: list = []


class _StubSockets:
    def get_clients(self):
        return []


class _StubBuilder:
    modules: list = []
    processors: list = []
    file_storage_provider = None
    filesystem_options = None


class TestAdminViaUsers:
    @pytest.fixture()
    def admin(self):
        result, users = _build(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000))
        admin_plugin = (
            CliAdminBuilder()
            .set_credentials("dashadmin", "dashpass")
            .set_users_plugin(users)
            .build(AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager,
                                  builder=result.builder))
        )
        result.mount_plugin(admin_plugin)
        with TestClient(result.app) as c:
            yield Server(c, users, result)

    def test_login_through_users(self, admin: Server):
        res = admin.c.post(BASE + "/auth/login", json={"username": "root", "password": ROOT_PASS})
        assert res.status_code == 200, res.text
        token = res.json()["token"]
        assert res.json()["username"] == "root"
        me = admin.c.get(BASE + "/auth/me", headers=_h(token))
        assert me.status_code == 200 and me.json()["username"] == "root"
        assert admin.c.get(BASE + "/status", headers=_h(token)).status_code == 200

    def test_static_credentials_ignored(self, admin: Server):
        res = admin.c.post(BASE + "/auth/login", json={"username": "dashadmin", "password": "dashpass"})
        assert res.status_code == 401

    def test_requires_admin_access(self, admin: Server):
        admin.user("alice")
        res = admin.c.post(BASE + "/auth/login", json={"username": "alice", "password": "pass1234"})
        assert res.status_code == 403
        alice = admin.login("alice", "pass1234")
        assert admin.c.get(BASE + "/status", headers=_h(alice)).status_code == 403
        admin.user("ops", ["admin:access"])
        assert admin.c.post(BASE + "/auth/login", json={"username": "ops", "password": "pass1234"}).status_code == 200

    def test_anonymous_admin_routes_denied(self, admin: Server):
        assert admin.c.get(BASE + "/status").status_code == 401


class TestAdminStandalone:
    def _client(self, trust_proxy: bool = False) -> TestClient:
        builder = CliAdminBuilder().set_credentials("dashadmin", "dashpass")
        if trust_proxy:
            builder.set_trust_proxy()
        plugin = builder.build(AdminBuildDeps(registry=_StubRegistry(), event_socket_manager=_StubSockets(),
                                              builder=_StubBuilder()))
        app = FastAPI()
        app.include_router(plugin.router, prefix=BASE)
        return TestClient(app)

    def test_xff_not_trusted_by_default(self):
        c = self._client()
        for i in range(5):
            c.post(BASE + "/auth/login", json={"username": "x", "password": "y"},
                   headers={"X-Forwarded-For": f"10.0.0.{i}"})
        res = c.post(BASE + "/auth/login", json={"username": "dashadmin", "password": "dashpass"},
                     headers={"X-Forwarded-For": "10.1.1.1"})
        assert res.status_code == 429

    def test_xff_trusted_with_trust_proxy(self):
        c = self._client(trust_proxy=True)
        for _ in range(5):
            c.post(BASE + "/auth/login", json={"username": "x", "password": "y"}, headers={"X-Forwarded-For": "10.0.0.1"})
        res = c.post(BASE + "/auth/login", json={"username": "dashadmin", "password": "dashpass"},
                     headers={"X-Forwarded-For": "10.0.0.2"})
        assert res.status_code == 200

    def test_non_ascii_credentials_do_not_crash(self):
        c = self._client()
        res = c.post(BASE + "/auth/login", json={"username": "dashadmin", "password": "pässwörd"})
        assert res.status_code == 401


# =========================================================================
# Same-origin requests always pass Origin checks
# =========================================================================

SELF = "http://testserver"  # TestClient's own origin (Host: testserver)


def _scope(scheme: str = "http", host: str | None = "localhost:8048", **headers: str) -> dict:
    raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    if host is not None:
        raw.append((b"host", host.encode()))
    return {"type": "http", "scheme": scheme, "headers": raw}


class TestSameOrigin:
    @pytest.mark.parametrize("fn", [core_is_same_origin, users_is_same_origin])
    def test_is_same_origin(self, fn):
        assert fn("http://localhost:8048", _scope())
        assert fn("HTTP://LocalHost:8048/", _scope())
        assert not fn("http://localhost:8049", _scope())
        assert not fn("https://localhost:8048", _scope())
        assert not fn("http://evil.test", _scope())
        assert not fn(None, _scope())
        assert not fn("null", _scope())
        assert not fn("http://localhost:8048", _scope(host=None))
        # default ports
        assert fn("http://example.com", _scope(host="example.com:80"))
        assert fn("https://example.com:443", _scope(scheme="https", host="EXAMPLE.com"))
        # websockets map ws/wss -> http/https
        assert fn("http://localhost:8048", _scope(scheme="ws"))
        assert fn("https://example.com", _scope(scheme="wss", host="example.com"))
        assert not fn("http://example.com", _scope(scheme="wss", host="example.com"))
        # IPv6
        assert fn("http://[::1]:8048", _scope(host="[::1]:8048"))
        # forwarded headers only with trust_proxy
        proxied = _scope(host="internal:8048", x_forwarded_proto="https", x_forwarded_host="cli.example.com")
        assert not fn("https://cli.example.com", proxied)
        assert fn("https://cli.example.com", proxied, True)
        assert not fn("http://internal:8048", proxied, True)

    def test_dashboard_login_same_origin(self):
        result, users = _build(
            CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000),
            allowed_origins=[ALLOWED],
        )
        admin_plugin = CliAdminBuilder().set_users_plugin(users).build(AdminBuildDeps(
            registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder))
        result.mount_plugin(admin_plugin)
        with TestClient(result.app) as c:
            res = c.post(BASE + "/auth/login", headers={"origin": SELF},
                         json={"username": "root", "password": ROOT_PASS})
            assert res.status_code == 200, res.text
            token = res.json()["token"]
            assert c.get(BASE + "/status", headers=_h(token, origin=SELF)).status_code == 200
            res = c.post(BASE + "/groups", headers=_h(token, origin=SELF), json={"name": "g-self"})
            assert res.status_code == 201, res.text

    @pytest.fixture()
    def ext(self):
        result, _ = _build(CliUsersBuilder().set_auth_adapter(_adapter), allowed_origins=[ALLOWED])
        with TestClient(result.app) as c:
            yield c

    def test_csrf_same_origin_passes(self, ext: TestClient):
        res = ext.post(BASE + "/groups", headers={"x-user": "boss", "origin": SELF}, json={"name": "g1"})
        assert res.status_code == 201, res.text
        res = ext.post("/api/qcli/execute", headers={"x-user": "boss", "origin": SELF}, json={"command": "echo"})
        assert res.status_code == 200, res.text
        res = ext.post(BASE + "/sessions/ws-ticket", headers={"x-user": "boss", "origin": SELF})
        assert res.status_code == 201, res.text

    def test_csrf_other_origin_still_rejected(self, ext: TestClient):
        for origin in ("http://evil.test", "http://testserver:8080", "https://testserver"):
            res = ext.post(BASE + "/groups", headers={"x-user": "boss", "origin": origin}, json={"name": "g1"})
            assert_error(res, 403, "origin not allowed")

    def test_csrf_forwarded_headers_need_trust_proxy(self):
        fwd = {"x-user": "boss", "origin": "https://cli.example.com",
               "x-forwarded-proto": "https", "x-forwarded-host": "cli.example.com"}
        result, _ = _build(CliUsersBuilder().set_auth_adapter(_adapter), allowed_origins=[ALLOWED])
        with TestClient(result.app) as c:
            assert_error(c.post(BASE + "/groups", headers=fwd, json={"name": "g1"}), 403, "origin not allowed")
        result, _ = _build(
            CliUsersBuilder().set_auth_adapter(_adapter).set_trust_proxy(), allowed_origins=[ALLOWED],
        )
        with TestClient(result.app) as c:
            assert c.post(BASE + "/groups", headers=fwd, json={"name": "g1"}).status_code == 201

    def test_ws_same_origin_passes(self):
        result, _ = _build(allowed_origins=[ALLOWED])
        with TestClient(result.app) as c:
            with c.websocket_connect("/ws/qcli/events", headers={"origin": SELF}) as ws:
                assert json.loads(ws.receive_text())["type"] == "connected"
            with c.websocket_connect("/ws/qcli/events", headers={"origin": "http://testserver:9"}) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
                assert exc.value.code == 4403

    def test_ws_same_origin_with_ticket(self, srv: Server):
        ticket = srv.ticket(srv.root)
        with srv.c.websocket_connect(f"/ws/qcli/events?ticket={ticket}", headers={"origin": SELF}) as ws:
            assert json.loads(ws.receive_text())["type"] == "connected"
