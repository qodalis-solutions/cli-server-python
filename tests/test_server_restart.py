"""Restarting the server: the pending list (mark_required / clear_required, events), config entries with
``requires_restart`` marked and cleared, supervisor detection, the modes (exit, self, handler, auto; the process
never exits or execs: the runtime is injected), 409 when not supported, 403 without sign-in, permissions on a
protected server, audit and events, the graceful shutdown on a real uvicorn server and on its own, and
``admin restart``."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Optional

import httpx
import pytest
import uvicorn
from fastapi import APIRouter
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from qodalis_cli import (
    RESTART_NOT_SUPPORTED,
    CliServerOptions,
    CliServerRestart,
    CliServerRestartOptions,
    create_cli_server,
    detect_supervisor,
)
from qodalis_cli.permissions import CORE_PERMISSIONS
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliStoredConfigProvider,
    cli_config_context,
)

from .command_helpers import V1, add_users, protect, run_command

URL = f"{V1}/server/restart"


class FakeRuntime:
    """Records what the process would do; no supervisor unless ``env`` or ``files`` say so."""

    def __init__(self, env: Optional[dict[str, str]] = None, files: Optional[dict[str, str]] = None) -> None:
        self.calls: list[str] = []
        self.execs: list[tuple[Any, ...]] = []
        self.env = env or {}
        self.files = files or {}
        self.exec_error: Optional[Exception] = None

    def exit(self, code: int) -> None:
        self.calls.append(f"exit {code}")

    def exec(self, executable: str, args: list[str], env: Any, cwd: str) -> None:
        self.calls.append("exec")
        self.execs.append((executable, args, env, cwd))
        if self.exec_error is not None:
            raise self.exec_error

    def runtime(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "exists": lambda p: p in self.files,
            "read_file": lambda p: self.files.get(p),
            "exit": self.exit,
            "exec": self.exec,
            "executable": "/usr/bin/python3",
            "argv": ["-X", "dev", "-m", "app.main", "--flag"],
            "cwd": lambda: "/app",
            "find_server": lambda: None,
        }


def start(fake: FakeRuntime, **restart: Any) -> Any:
    cli = create_cli_server(CliServerOptions(restart=CliServerRestartOptions(runtime=fake.runtime(), **restart)))
    return cli


def events(cli: Any, pattern: str = "server:*") -> list[Any]:
    seen: list[Any] = []
    cli.event_bus.subscribe(pattern, seen.append)
    return seen


def client(cli: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=cli.app), base_url="http://test")


class _Audit:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def log(self, entry: Any) -> None:
        self.entries.append(entry)


# --------------------------------------------------------------------------- the pending list


async def test_marks_once_per_id_clears_publishes_restart_required_and_is_empty_at_start():
    cli = start(FakeRuntime(), mode="exit")
    seen = events(cli)
    assert cli.restart.pending() == []
    cli.restart.mark_required("plugin:x", "x", "X installed")
    cli.restart.mark_required("plugin:x", "x", "X installed again")
    cli.restart.mark_required("plugin:y", "y", "Y updated")
    assert [(p.id, p.description) for p in cli.restart.pending()] == [("plugin:x", "X installed"), ("plugin:y", "Y updated")]
    assert re.match(r"^\d{4}-\d\d-\d\dT", cli.restart.pending()[0].since)
    assert cli.restart.clear_required("plugin:x") is True
    assert cli.restart.clear_required("plugin:x") is False
    await asyncio.sleep(0.01)
    assert [(e.type, len(e.data["pending"]), e.audience.permission) for e in seen] == [
        ("server:restart-required", 1, "admin:access"),
        ("server:restart-required", 2, "admin:access"),
        ("server:restart-required", 1, "admin:access"),
    ]
    with pytest.raises(ValueError, match="mark_required needs"):
        cli.restart.mark_required("", "x", "d")
    # the builder's service is the same one
    assert cli.builder.restart is cli.restart


async def test_marks_a_changed_requires_restart_setting_and_clears_it_when_set_back_to_the_value_at_start():
    cli = start(FakeRuntime(), mode="exit")
    cli.builder.add_config_provider(CliStoredConfigProvider(id="app", title="App", sections=[
        CliConfigSectionDefinition(id="main", title="Main", settings=[
            CliConfigSettingDefinition(key="port", label="Listen port", type="number", default=80, requires_restart=True),
            CliConfigSettingDefinition(key="name", type="string", default="x"),
        ]),
    ]))
    ctx = cli_config_context(None, "api")
    await cli.config.update("app", "main", {"changes": {"name": "y"}}, ctx)
    assert cli.restart.pending() == []
    await cli.config.update("app", "main", {"changes": {"port": 81}}, ctx)
    assert [(p.id, p.source, p.description) for p in cli.restart.pending()] == [
        ("config:app.main.port", "config", "App: Listen port changed"),
    ]
    await cli.config.update("app", "main", {"changes": {"port": 82}}, ctx)
    assert len(cli.restart.pending()) == 1
    await cli.config.update("app", "main", {"changes": {"port": 80}}, ctx)
    assert cli.restart.pending() == []


# --------------------------------------------------------------------------- supervisor detection


def test_finds_pm2_systemd_kubernetes_and_docker_from_the_environment_and_files():
    def detect(env: dict[str, str], files: Optional[dict[str, str]] = None) -> Optional[str]:
        return detect_supervisor(type("R", (), {
            "env": env, "exists": staticmethod(lambda p: p in (files or {})), "read_file": staticmethod(lambda p: (files or {}).get(p)),
        }))

    assert detect({}) is None
    assert detect({"pm_id": "0"}) == "pm2"
    assert detect({"INVOCATION_ID": "abc"}) == "systemd"
    assert detect({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}) == "kubernetes"
    assert detect({}, {"/proc/1/cgroup": "0::/kubepods/besteffort/pod1"}) == "kubernetes"
    assert detect({}, {"/.dockerenv": ""}) == "docker"
    assert detect({}, {"/proc/1/cgroup": "12:cpu:/docker/abc"}) == "docker"
    assert detect({"pm_id": "1"}, {"/.dockerenv": ""}) == "pm2"


# --------------------------------------------------------------------------- the modes


async def test_auto_without_a_supervisor_not_supported_get_says_why_post_409():
    fake = FakeRuntime()
    cli = start(fake, allow_anonymous=True)
    async with client(cli) as c:
        res = await c.get(URL)
        assert res.status_code == 200
        assert res.json() == {
            "supported": False, "mode": None, "supervisor": None,
            "reason": "no supervisor detected; set restart.mode", "startedAt": cli.restart.started_at, "pending": [],
        }
        post = await c.post(URL, json={})
        assert post.status_code == 409
        assert post.json() == {"error": RESTART_NOT_SUPPORTED}
        assert post.json()["error"] == "restart not supported: no supervisor detected; set restart.mode"
    assert fake.calls == []


async def test_auto_under_systemd_exits_with_75_after_the_graceful_shutdown():
    fake = FakeRuntime({"INVOCATION_ID": "x"})
    cli = start(fake, allow_anonymous=True)
    async with client(cli) as c:
        status = (await c.get(URL)).json()
        assert (status["supported"], status["mode"], status["supervisor"]) == (True, "exit", "systemd")
        res = await c.post(URL, json={"reason": "apply settings"})
        assert res.status_code == 202
        assert res.json() == {"restarting": True, "pending": []}
        await cli.restart.completion
    assert fake.calls == ["exit 75"]


async def test_exit_with_a_custom_exit_code_the_supervisor_reported_with_an_explicit_mode():
    fake = FakeRuntime(files={"/.dockerenv": ""})
    cli = start(fake, mode="exit", exit_code=3, allow_anonymous=True)
    status = cli.restart.status()
    assert (status["supported"], status["mode"], status["supervisor"]) == (True, "exit", "docker")
    async with client(cli) as c:
        assert (await c.post(URL)).status_code == 202
        await cli.restart.completion
    assert fake.calls == ["exit 3"]


async def test_self_runs_the_same_command_again_executable_arguments_cwd_and_env():
    env = {"PORT": "8048", "QCLI_DEMO_CONFIG": "/tmp/c.json"}
    fake = FakeRuntime(env)
    cli = start(fake, mode="self", allow_anonymous=True)
    status = cli.restart.status()
    assert (status["supported"], status["mode"], status["supervisor"]) == (True, "self", None)
    async with client(cli) as c:
        assert (await c.post(URL)).status_code == 202
        await cli.restart.completion
    assert fake.execs == [("/usr/bin/python3", ["/usr/bin/python3", "-X", "dev", "-m", "app.main", "--flag"], env, "/app")]
    assert fake.calls == ["exec"]


async def test_self_exits_1_when_the_command_cannot_run_again():
    fake = FakeRuntime()
    fake.exec_error = OSError("ENOENT")
    cli = start(fake, mode="self", allow_anonymous=True)
    async with client(cli) as c:
        assert (await c.post(URL)).status_code == 202
        await cli.restart.completion
    assert fake.calls == ["exec", "exit 1"]


async def test_handler_the_hosts_function_after_the_shutdown_with_the_reason_pending_ids_and_who_asked():
    fake = FakeRuntime()
    received: list[Any] = []

    async def handler(context: Any) -> None:
        received.append(context)

    cli = start(fake, mode="handler", handler=handler, allow_anonymous=True)
    cli.restart.mark_required("a", "s", "A")
    async with client(cli) as c:
        assert (await c.post(URL, json={"reason": "  why  "})).status_code == 202
        await cli.restart.completion
    assert [(r.reason, r.pending, r.by) for r in received] == [("why", ["a"], "anonymous")]
    assert fake.calls == []


def test_refuses_a_handler_mode_without_a_handler_and_an_unknown_mode():
    with pytest.raises(ValueError, match="restart.mode 'handler' needs restart.handler"):
        create_cli_server(CliServerOptions(restart={"mode": "handler"}))
    with pytest.raises(ValueError, match="invalid restart.mode: nope"):
        create_cli_server(CliServerOptions(restart={"mode": "nope"}))


async def test_validates_the_reason_and_refuses_a_second_restart():
    cli = start(FakeRuntime(), mode="handler", handler=lambda _c: None, allow_anonymous=True)
    async with client(cli) as c:
        assert (await c.post(URL, json={"reason": 5})).json() == {"error": "reason must be a string"}
        assert (await c.post(URL, json={"reason": "x" * 501})).status_code == 400
        assert (await c.post(URL)).status_code == 202
        again = await c.post(URL)
        assert again.status_code == 409
        assert again.json() == {"error": "restart already in progress"}
        assert cli.restart.restarting is True
        await cli.restart.completion


async def test_refuses_with_403_on_a_server_without_sign_in_unless_allow_anonymous():
    fake = FakeRuntime()
    cli = start(fake, mode="exit")
    async with client(cli) as c:
        res = await c.post(URL)
        assert res.status_code == 403
        assert res.json()["error"].startswith("restart needs sign-in")
        # the status stays readable
        assert (await c.get(URL)).json()["supported"] is True
    assert fake.calls == []


# --------------------------------------------------------------------------- protected server


def test_get_needs_admin_access_post_server_restart_audits_and_publishes_restarting():
    fake = FakeRuntime()
    cli = start(fake, mode="exit")
    audit = _Audit()
    cli.builder.set_config_audit_store(audit)
    protect(cli)
    seen = events(cli, "server:restarting")
    cli.restart.mark_required("config:cors.cors.mode", "config", "CORS: Mode changed")
    with TestClient(cli.app) as c:
        tokens = add_users(c, {"viewer": ["admin:access"], "restarter": ["server:restart"]})

        def auth(user: str) -> dict[str, str]:
            return {"Authorization": f"Bearer {tokens[user]}"}

        assert c.get(URL).status_code == 401
        assert c.get(URL, headers=auth("restarter")).status_code == 403
        status = c.get(URL, headers=auth("viewer"))
        assert status.status_code == 200
        assert [(p["id"], p["description"]) for p in status.json()["pending"]] == [("config:cors.cors.mode", "CORS: Mode changed")]
        refused = c.post(URL, headers=auth("viewer"))
        assert refused.status_code == 403
        assert refused.json() == {"error": "missing permission: server:restart"}

        res = c.post(URL, headers=auth("restarter"), json={"reason": "apply CORS"})
        assert res.status_code == 202
        assert res.json() == {"restarting": True, "pending": ["config:cors.cors.mode"]}
        c.portal.call(_wait, cli.restart)
    assert fake.calls == ["exit 75"]

    entry = next(e for e in audit.entries if e.event == "server.restart")
    assert (entry.username, entry.actor, entry.target, entry.details) == ("restarter", "restarter", "server", "apply CORS")
    assert entry.properties == {"reason": "apply CORS", "by": "restarter", "pending": ["config:cors.cors.mode"], "mode": "exit", "via": "api"}
    assert len(seen) == 1
    assert seen[0].data == {"reason": "apply CORS", "by": "restarter", "pending": ["config:cors.cors.mode"], "mode": "exit"}
    assert seen[0].audience.permission == "admin:access"


async def _wait(restart: Any) -> None:
    if restart.completion is not None:
        await asyncio.wait_for(asyncio.shield(restart.completion), 10)


def test_lists_server_restart_in_the_catalog_group_server():
    entry = next(p for p in CORE_PERMISSIONS if p.name == "server:restart")
    assert (entry.description, entry.group) == ("Restart the server", "Server")
    cli = start(FakeRuntime(), mode="exit")
    found = cli.builder.permissions.find("server:restart")
    assert (found.group, found.surface) == ("Server", "cli")


# --------------------------------------------------------------------------- graceful shutdown


def test_on_a_real_uvicorn_server_finishes_in_flight_requests_closes_sockets_with_1012_runs_the_hooks_then_exits():
    from websockets.sync.client import connect

    fake = FakeRuntime()
    cli = start(fake, mode="exit", allow_anonymous=True)
    order = fake.calls

    @cli.app.get("/slow")
    async def slow() -> str:
        await asyncio.sleep(0.3)
        order.append("slow done")
        return "ok"

    cli.restart.on_shutdown("host", lambda: order.append("hook"))

    class _Scheduler:
        async def stop(self) -> None:
            order.append("scheduler stopped")

    class _Plugin:
        prefix = "/api/v1/qcli/fake"
        router = APIRouter()
        scheduler = _Scheduler()

        def dispose(self) -> None:
            order.append("plugin disposed")

    cli.mount_plugin(_Plugin())

    server = uvicorn.Server(uvicorn.Config(cli.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    cli.restart.attach_server(server)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        ws = connect(f"ws://127.0.0.1:{port}/ws/v1/qcli/events")
        slow: dict[str, Any] = {}
        slow_thread = threading.Thread(target=lambda: slow.update(res=httpx.get(f"{base}/slow", timeout=10)))
        slow_thread.start()
        time.sleep(0.1)
        assert httpx.post(f"{base}{URL}", timeout=10).status_code == 202

        thread.join(15)
        slow_thread.join(10)
        assert not thread.is_alive()
        assert slow["res"].text == '"ok"'
        with pytest.raises(Exception):
            while True:
                ws.recv(timeout=5)
        assert ws.close_code == 1012
        # the hooks in registration order (the host's was added before the plugin was mounted), then the exit
        assert order == ["slow done", "hook", "scheduler stopped", "plugin disposed", "exit 75"]
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{base}/api/qcli/versions", timeout=2)
    finally:
        server.should_exit = True
        thread.join(10)


async def test_on_its_own_waits_for_in_flight_requests_closes_sockets_and_stops_after_the_drain_timeout():
    fake = FakeRuntime()
    cli = start(fake, mode="exit", allow_anonymous=True, drain_timeout=0.1)
    never = asyncio.Event()

    @cli.app.get("/hang")
    async def hang() -> str:
        await never.wait()
        return "late"

    closed: list[tuple[int, str]] = []
    original = cli.event_socket_manager.broadcast_disconnect

    async def broadcast(code: int = 1000, reason: str = "") -> None:
        closed.append((code, reason))
        await original(code, reason)

    cli.event_socket_manager.broadcast_disconnect = broadcast  # type: ignore[method-assign]
    async with client(cli) as c:
        hung = asyncio.ensure_future(c.get("/hang"))
        await asyncio.sleep(0.05)
        assert (await c.post(URL)).status_code == 202
        started = time.monotonic()
        await cli.restart.completion
        assert time.monotonic() - started < 3
        hung.cancel()
    assert closed == [(1012, "Server restarting")]
    assert fake.calls == ["exit 75"]


async def test_the_service_on_its_own_runs_the_hooks_in_order_errors_logged():
    restart = CliServerRestart().configure({"mode": "exit", "runtime": FakeRuntime().runtime()})
    ran: list[str] = []

    async def c() -> None:
        ran.append("c")

    def boom() -> None:
        raise RuntimeError("boom")

    restart.on_shutdown("a", lambda: ran.append("a"))
    restart.on_shutdown("b", boom)
    restart.on_shutdown("c", c)
    await restart.shutdown()
    assert ran == ["a", "c"]


def test_a_normal_shutdown_does_not_restart():
    fake = FakeRuntime()
    cli = start(fake, mode="exit", allow_anonymous=True)
    cli.restart.on_shutdown("host", lambda: fake.calls.append("hook"))
    with TestClient(cli.app) as c:
        assert c.get(URL).status_code == 200
    assert fake.calls == []


# --------------------------------------------------------------------------- admin restart


class _AdminHost:
    def __init__(self, fake: FakeRuntime, users: dict[str, list[str]], **restart: Any) -> None:
        self.cli = start(fake, **restart)
        self.audit = _Audit()
        self.cli.builder.set_config_audit_store(self.audit)
        self.users = protect(self.cli)
        admin = CliAdminBuilder().set_users_plugin(self.users).set_jwt_secret("restart-test-secret-0123456789abcdef")
        admin.set_marketplace(False)
        self.admin = admin.build(AdminBuildDeps(
            registry=self.cli.registry, event_socket_manager=self.cli.event_socket_manager, builder=self.cli.builder,
        ))
        self.admin.log_buffer.uninstall_handler()
        self.cli.mount_plugin(self.admin)
        self.client = TestClient(self.cli.app)
        self.client.__enter__()
        self.tokens = add_users(self.client, users)

    def run(self, line: str, user: str = "root") -> Any:
        return run_command(self.client, self.cli, line, self.tokens[user])

    def wait(self) -> None:
        self.client.portal.call(_wait, self.cli.restart)

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        self.admin.log_buffer.uninstall_handler()


@pytest.fixture
def admin_host():
    hosts: list[_AdminHost] = []

    def make(fake: FakeRuntime, users: Optional[dict[str, list[str]]] = None, **restart: Any) -> _AdminHost:
        host = _AdminHost(fake, users or {}, **restart)
        hosts.append(host)
        return host

    yield make
    for h in hosts:
        h.close()


def test_admin_restart_without_yes_prints_what_is_pending_and_how_to_confirm_with_yes_restarts(admin_host):
    fake = FakeRuntime()
    h = admin_host(fake, mode="self")
    h.cli.restart.mark_required("config:cors.cors.mode", "config", "CORS: Mode changed")

    preview = h.run("admin restart").text
    assert "1 change(s) take effect after a restart:" in preview
    assert "  - CORS: Mode changed (config, since " in preview
    assert "Restart mode: self (starts itself again)" in preview
    assert 'Run admin restart --yes [--reason "<why>"] to restart the server now.' in preview
    assert fake.calls == []

    status = h.run("admin restart status").text
    assert re.match(r"^Running since \d{4}-\d\d-\d\d \d\d:\d\d:\d\d\nRestart mode: self", status)
    data = json.loads(h.run("admin restart status --json").text)
    assert (data["supported"], data["mode"], data["pending"][0]["id"]) == (True, "self", "config:cors.cors.mode")

    done = h.run('admin restart --yes --reason "apply CORS"').text
    assert done == (
        "Restarting the server to apply 1 pending change(s) (self mode). "
        "The connection drops and comes back once it runs again."
    )
    h.wait()
    assert fake.calls == ["exec"]
    entry = next(e for e in h.audit.entries if e.event == "server.restart")
    assert (entry.properties["reason"], entry.properties["by"], entry.properties["via"]) == ("apply CORS", "root", "command")


def test_admin_restart_status_needs_only_admin_access_restarting_needs_server_restart(admin_host):
    fake = FakeRuntime()
    h = admin_host(fake, {"viewer": ["admin:access", "commands:execute"]}, mode="exit")
    assert "No changes are waiting for a restart." in h.run("admin restart status", "viewer").text
    assert h.run("admin restart --yes", "viewer").text == "Error: forbidden: missing permission server:restart"
    assert fake.calls == []


def test_admin_restart_says_when_restarting_is_not_supported(admin_host):
    h = admin_host(FakeRuntime())
    assert "Error: restart not supported: no supervisor detected; set restart.mode" in h.run("admin restart --yes").text
    assert "Restart from here: not supported" in h.run("admin restart status").text
