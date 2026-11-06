"""Server events: the event bus, audience delivery on the events WebSocket
and the event catalog (spec 2026-11-06-server-events-design)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import (
    CliEchoCommandProcessor,
    CliEvent,
    CliEventAudience,
    CliEventBus,
    CliServerOptions,
    ICliEventBus,
    InMemoryFileStorageProvider,
    cli_event_pattern_matches,
    create_cli_server,
)
from qodalis_cli.filesystem.event_publishing_provider import EventPublishingFileStorageProvider
from qodalis_cli_audit import AuditEntry, CliAuditBuilder
from qodalis_cli_jobs import CliJobScheduler, CliJobsBuilder, InMemoryJobStorageProvider
from qodalis_cli_server_abstractions import CliCommandProcessor, ICliStreamCommandProcessor
from qodalis_cli_server_abstractions.jobs import CliJobOptions, ICliJob
from qodalis_cli_users import CliUsersBuilder

BASE = "/api/v1/qcli"
EVENTS = "/ws/v1/qcli/events"
MCP = "/api/qcli/mcp"
ROOT_PASS = "rootpass1"
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _h(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


# =========================================================================
# The bus
# =========================================================================


class TestPatternMatching:
    @pytest.mark.parametrize("event_type,pattern,expected", [
        ("job:completed", "*", True),
        ("anything", "*", True),
        ("job:completed", "job:*", True),
        ("job:runs-deleted", "job:*", True),
        ("jobs:completed", "job:*", False),
        ("job", "job:*", False),
        ("job:completed", "job:completed", True),
        ("job:completed", "job:failed", False),
        ("job:completed", "job", False),
        ("user:created", "job:*", False),
    ])
    def test_matches(self, event_type: str, pattern: str, expected: bool):
        assert cli_event_pattern_matches(event_type, pattern) is expected


class TestBus:
    def test_is_an_event_bus(self):
        assert isinstance(CliEventBus(), ICliEventBus)

    async def test_handlers_run_in_order_with_matching_patterns(self):
        bus = CliEventBus()
        calls: list[str] = []

        async def async_handler(event: CliEvent) -> None:
            calls.append(f"async:{event.type}")

        bus.subscribe("job:*", lambda e: calls.append(f"jobs:{e.type}"))
        bus.subscribe("*", async_handler)
        bus.subscribe("user:created", lambda e: calls.append(f"exact:{e.type}"))

        await bus.publish("job:completed", {"jobId": "a"})
        await bus.publish("user:created", {"userId": "u1"})
        assert calls == [
            "jobs:job:completed", "async:job:completed",
            "async:user:created", "exact:user:created",
        ]

    async def test_unsubscribe(self):
        bus = CliEventBus()
        calls: list[str] = []
        handler = lambda e: calls.append(e.type)  # noqa: E731
        unsubscribe = bus.subscribe("*", handler)
        bus.subscribe("*", handler)
        unsubscribe()
        unsubscribe()  # a second call is a no-op
        await bus.publish("x:y")
        assert calls == ["x:y"]

    async def test_handler_errors_are_isolated(self, caplog: pytest.LogCaptureFixture):
        bus = CliEventBus()
        calls: list[str] = []

        def boom(event: CliEvent) -> None:
            raise RuntimeError("sync boom")

        async def async_boom(event: CliEvent) -> None:
            raise RuntimeError("async boom")

        bus.subscribe("*", boom)
        bus.subscribe("*", async_boom)
        bus.subscribe("*", lambda e: calls.append(e.type))
        with caplog.at_level(logging.ERROR):
            await bus.publish("job:failed", {"jobId": "a"})  # does not raise
        assert calls == ["job:failed"]
        assert "sync boom" in caplog.text and "async boom" in caplog.text

    async def test_event_shape_and_flat_wire_format(self):
        bus = CliEventBus()
        events: list[CliEvent] = []
        bus.subscribe("*", events.append)
        audience = CliEventAudience(permission="jobs:read", user_id="u1")
        await bus.publish("job:completed", {"jobId": "a", "duration": 5, "type": "ignored"}, audience)

        event = events[0]
        assert event.type == "job:completed"
        assert ISO_UTC.match(event.timestamp), event.timestamp
        assert event.data == {"jobId": "a", "duration": 5}
        assert event.audience == audience
        assert event.to_message() == {
            "type": "job:completed", "timestamp": event.timestamp, "jobId": "a", "duration": 5,
        }

    async def test_publisher_timestamp_is_kept(self):
        bus = CliEventBus()
        events: list[CliEvent] = []
        bus.subscribe("*", events.append)
        await bus.publish("a:b", {"timestamp": "2026-11-06T10:15:00.000Z"})
        await bus.publish("a:b")
        assert events[0].timestamp == "2026-11-06T10:15:00.000Z"
        assert events[0].data == {}
        assert events[1].audience.is_everyone

    def test_audience_helpers(self):
        assert CliEventAudience.everyone().is_everyone
        assert CliEventAudience.with_permission("p") == CliEventAudience(permission="p")
        assert CliEventAudience.user("u") == CliEventAudience(user_id="u")

    def test_server_exposes_the_bus(self):
        result = create_cli_server()
        assert isinstance(result.event_bus, CliEventBus)
        assert result.executor.event_bus is result.event_bus


# =========================================================================
# Delivery over the events WebSocket
# =========================================================================

MARKER = "test:marker"


def _publish(client: TestClient, bus: CliEventBus, event_type: str, data: dict | None = None,
             audience: CliEventAudience | None = None) -> None:
    client.portal.call(bus.publish, event_type, data, audience)


def _received(ws) -> list[dict[str, Any]]:
    """Messages up to the marker (published to everyone after the events
    under test), without it."""
    messages: list[dict[str, Any]] = []
    while True:
        message = json.loads(ws.receive_text())
        if message["type"] == MARKER:
            return messages
        messages.append(message)


def _types(messages: list[dict[str, Any]], prefix: str = "test:") -> list[str]:
    return [m["type"] for m in messages if m["type"].startswith(prefix)]


class TestUnprotectedDelivery:
    def test_every_event_reaches_every_socket(self):
        result = create_cli_server()
        with TestClient(result.app) as client:
            with client.websocket_connect(EVENTS) as a, client.websocket_connect("/ws/qcli/events") as b:
                for ws in (a, b):
                    assert json.loads(ws.receive_text()) == {"type": "connected"}
                bus = result.event_bus
                _publish(client, bus, "test:everyone", {"n": 1})
                _publish(client, bus, "test:permission", {"n": 2}, CliEventAudience(permission="jobs:read"))
                _publish(client, bus, "test:user", {"n": 3}, CliEventAudience(user_id="u1"))
                _publish(client, bus, MARKER)
                for ws in (a, b):
                    messages = _received(ws)
                    assert _types(messages) == ["test:everyone", "test:permission", "test:user"]
                    first = messages[0]
                    assert set(first) == {"type", "timestamp", "n"}
                    assert ISO_UTC.match(first["timestamp"])

    def test_broadcast_message_still_works(self):
        result = create_cli_server()
        with TestClient(result.app) as client:
            with client.websocket_connect(EVENTS) as ws:
                ws.receive_text()
                client.portal.call(result.event_socket_manager.broadcast_message, '{"type":"legacy"}')
                assert json.loads(ws.receive_text()) == {"type": "legacy"}


class _Server:
    def __init__(self, client: TestClient, result: Any, users: Any) -> None:
        self.c = client
        self.result = result
        self.users = users

    def login(self, name: str, password: str = "pass1234") -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": password})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    @property
    def root(self) -> str:
        return self.login("root", ROOT_PASS)

    def create_user(self, name: str, perms: list[str] | None = None) -> dict[str, Any]:
        admin = self.root
        groups = ["users"]
        if perms:
            res = self.c.post(BASE + "/groups", headers=_h(admin), json={"name": f"{name}-g", "permissions": perms})
            assert res.status_code == 201, res.text
            groups.append(f"{name}-g")
        res = self.c.post(BASE + "/users", headers=_h(admin), json={
            "name": name, "email": f"{name}@example.com", "password": "pass1234", "groups": groups,
        })
        assert res.status_code == 201, res.text
        return res.json()

    def events(self, token: str):
        res = self.c.post(BASE + "/sessions/ws-ticket", headers=_h(token))
        assert res.status_code == 201, res.text
        return self.c.websocket_connect(f"{EVENTS}?ticket={res.json()['ticket']}")

    def publish(self, event_type: str, data: dict | None = None,
                audience: CliEventAudience | None = None) -> None:
        _publish(self.c, self.result.event_bus, event_type, data, audience)


def _protected_server(**opts: Any):
    def configure(builder):
        builder.add_processor(CliEchoCommandProcessor())
        builder.add_processor(StreamProcessor())
        builder.set_file_storage_provider(InMemoryFileStorageProvider())

    result = create_cli_server(CliServerOptions(configure=configure, **opts))
    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
    result.mount_plugin(users)
    return result, users


@pytest.fixture()
def srv():
    result, users = _protected_server(mcp=True)
    with TestClient(result.app) as client:
        yield _Server(client, result, users)


class TestAudienceDelivery:
    def test_permission_user_and_either_or(self, srv: _Server):
        alice = srv.create_user("alice", ["jobs:read"])
        bob = srv.create_user("bob")
        tokens = {"root": srv.root, "alice": srv.login("alice"), "bob": srv.login("bob")}
        with srv.events(tokens["root"]) as root_ws, srv.events(tokens["alice"]) as alice_ws, \
                srv.events(tokens["bob"]) as bob_ws:
            sockets = {"root": root_ws, "alice": alice_ws, "bob": bob_ws}
            for ws in sockets.values():
                assert json.loads(ws.receive_text())["type"] == "connected"
            srv.publish("test:everyone")
            srv.publish("test:jobs", audience=CliEventAudience(permission="jobs:read"))
            srv.publish("test:bob", audience=CliEventAudience(user_id=bob["id"]))
            srv.publish("test:alice", audience=CliEventAudience(user_id=alice["id"]))
            srv.publish("test:either", audience=CliEventAudience(permission="jobs:read", user_id=bob["id"]))
            srv.publish(MARKER)
            received = {name: _types(_received(ws)) for name, ws in sockets.items()}

        assert received["root"] == ["test:everyone", "test:jobs", "test:either"]
        assert received["alice"] == ["test:everyone", "test:jobs", "test:alice", "test:either"]
        assert received["bob"] == ["test:everyone", "test:bob", "test:either"]

    def test_catalog_events_respect_their_audience(self, srv: _Server):
        srv.create_user("bob")
        bob_token = srv.login("bob")
        root = srv.root
        with srv.events(root) as root_ws, srv.events(bob_token) as bob_ws:
            root_ws.receive_text()
            bob_ws.receive_text()
            res = srv.c.post(BASE + "/users", headers=_h(root), json={
                "name": "carol", "email": "carol@example.com", "password": "pass1234",
            })
            assert res.status_code == 201
            srv.publish(MARKER)
            root_messages = _received(root_ws)
            bob_messages = _received(bob_ws)

        created = [m for m in root_messages if m["type"] == "user:created"]
        assert created == [{
            "type": "user:created", "timestamp": created[0]["timestamp"],
            "userId": res.json()["id"], "username": "carol", "actor": "root",
        }]
        assert not _types(bob_messages, "user:")
        assert not _types(bob_messages, "audit:")


# =========================================================================
# The catalog: each event fires from its action
# =========================================================================


class Recorder:
    def __init__(self, bus: CliEventBus) -> None:
        self.events: list[CliEvent] = []
        bus.subscribe("*", self.events.append)

    def of(self, event_type: str) -> list[CliEvent]:
        return [e for e in self.events if e.type == event_type]

    def one(self, event_type: str) -> CliEvent:
        found = self.of(event_type)
        assert len(found) == 1, (event_type, [e.type for e in self.events])
        return found[0]

    def clear(self) -> None:
        self.events.clear()


class StreamProcessor(CliCommandProcessor, ICliStreamCommandProcessor):
    command = "ticker"
    description = "Streams two lines"

    async def handle_async(self, command, cancellation_event=None) -> str:
        return "tick"

    async def handle_stream_async(self, command, emit, cancellation_event=None) -> int:
        emit({"type": "text", "value": "tick"})
        return 0


class TestUsersEvents:
    def test_user_group_and_membership_events(self, srv: _Server):
        rec = Recorder(srv.result.event_bus)
        root = srv.root
        c = srv.c
        res = c.post(BASE + "/users", headers=_h(root), json={"name": "dave", "email": "dave@example.com"})
        user_id = res.json()["id"]
        event = rec.one("user:created")
        assert event.data == {"userId": user_id, "username": "dave", "actor": "root"}
        assert event.audience == CliEventAudience(permission="users:read")

        rec.clear()
        assert c.put(BASE + "/users/dave", headers=_h(root), json={"disabled": True}).status_code == 200
        assert rec.one("user:updated").data["userId"] == user_id

        rec.clear()
        assert c.post(BASE + "/groups", headers=_h(root), json={"name": "ops"}).status_code == 201
        event = rec.one("group:created")
        assert event.data == {"group": "ops", "actor": "root"}
        assert event.audience == CliEventAudience(permission="groups:read")
        assert c.put(BASE + "/groups/ops", headers=_h(root), json={"description": "Ops"}).status_code == 200
        assert rec.one("group:updated").data["group"] == "ops"

        rec.clear()
        assert c.post(BASE + "/groups/ops/members/dave", headers=_h(root)).status_code == 204
        assert rec.one("user:updated").data["username"] == "dave"
        rec.clear()
        assert c.delete(BASE + "/groups/ops/members/dave", headers=_h(root)).status_code == 204
        assert rec.one("user:updated").data["username"] == "dave"
        assert c.post(BASE + "/groups/ops/members/dave", headers=_h(root)).status_code == 204

        rec.clear()
        assert c.delete(BASE + "/groups/ops", headers=_h(root)).status_code == 204
        assert rec.one("group:deleted").data["group"] == "ops"
        assert rec.one("user:updated").data["username"] == "dave"  # lost the membership

        rec.clear()
        assert c.delete(BASE + "/users/dave", headers=_h(root)).status_code == 204
        assert rec.one("user:deleted").data == {"userId": user_id, "username": "dave", "actor": "root"}

    def test_password_change_is_a_user_update(self, srv: _Server):
        srv.create_user("erin")
        rec = Recorder(srv.result.event_bus)
        res = srv.c.put(BASE + "/users/erin/password", headers=_h(srv.root), json={"password": "newpass99"})
        assert res.status_code in (200, 204), res.text
        assert rec.one("user:updated").data["username"] == "erin"

    def test_token_events(self, srv: _Server):
        rec = Recorder(srv.result.event_bus)
        root = srv.root
        res = srv.c.post(BASE + "/tokens", headers=_h(root), json={"name": "ci"})
        assert res.status_code == 201, res.text
        token = res.json()["apiToken"]
        created = rec.one("token:created")
        owner = created.data["userId"]
        assert created.data == {"tokenId": token["id"], "name": "ci", "userId": owner, "actor": "root"}
        assert created.audience == CliEventAudience(permission="tokens:read", user_id=owner)

        assert srv.c.delete(f"{BASE}/tokens/{token['id']}", headers=_h(root)).status_code == 204
        revoked = rec.one("token:revoked")
        assert revoked.data == {"tokenId": token["id"], "name": "ci", "userId": owner, "actor": "root"}
        assert revoked.audience == CliEventAudience(permission="tokens:read", user_id=owner)

    def test_session_events(self, srv: _Server):
        rec = Recorder(srv.result.event_bus)
        token = srv.root
        opened = rec.one("session:opened")
        assert opened.audience == CliEventAudience(permission="sessions:read")
        assert opened.data["username"] == "root"
        session_id = opened.data["sessionId"]

        assert srv.c.delete(BASE + "/sessions/current", headers=_h(token)).status_code == 204
        closed = rec.one("session:closed")
        assert closed.data == {
            "userId": opened.data["userId"], "username": "root", "sessionId": session_id, "actor": "root",
        }

        # A revoke (kick) closes the session too.
        rec.clear()
        admin = srv.root
        srv.root  # a second session, revoked by the first
        other_id = rec.of("session:opened")[-1].data["sessionId"]
        assert srv.c.delete(f"{BASE}/sessions/{other_id}", headers=_h(admin)).status_code == 204
        assert rec.one("session:closed").data["sessionId"] == other_id

    async def test_expired_sessions_publish_session_closed(self):
        result, users = _protected_server()
        rec = Recorder(result.event_bus)
        auth = users.auth_service
        await users.ensure_seeded()
        login = await auth.login("root", ROOT_PASS)
        auth.options.session_timeout_ms = -1  # every session has expired
        await auth.cleanup_sessions()
        closed = rec.one("session:closed")
        assert closed.data == {"userId": login.user.id, "username": "root", "sessionId": login.session.id}


class TestAuditEvents:
    def test_audit_recorded_for_users_plugin_entries(self, srv: _Server):
        rec = Recorder(srv.result.event_bus)
        srv.root
        recorded = [e for e in rec.of("audit:recorded") if e.data["event"] == "login"]
        assert len(recorded) == 1
        event = recorded[0]
        assert event.audience == CliEventAudience(permission="audit:read")
        assert event.data["username"] == "root" and "id" in event.data
        assert ISO_UTC.match(event.timestamp)
        # The same entry as the audit API returns it (its time is the event's).
        listed = srv.c.get(BASE + "/audit", headers=_h(srv.root)).json()
        entry = next(e for e in listed if e["id"] == event.data["id"])
        assert {**entry, "timestamp": None} == {**event.data, "timestamp": None}

    def test_audit_plugin_log_and_clear(self):
        result = create_cli_server()
        audit = CliAuditBuilder().build()
        result.mount_plugin(audit)
        rec = Recorder(result.event_bus)
        with TestClient(result.app) as client:
            client.portal.call(audit.log, AuditEntry(event="setting_changed", username="host"))
            assert rec.one("audit:recorded").data["event"] == "setting_changed"
            rec.clear()
            assert client.delete(BASE + "/audit").status_code == 204
            assert rec.one("audit:recorded").data["event"] == "audit_cleared"


class TestCommandEvents:
    def test_rest_execute_stream_and_mcp(self, srv: _Server):
        rec = Recorder(srv.result.event_bus)
        root = srv.root
        user_id = rec.one("session:opened").data["userId"]

        res = srv.c.post(BASE + "/execute", headers=_h(root), json={"command": "echo", "value": "secret-input"})
        assert res.status_code == 200
        event = rec.one("command:executed")
        assert event.audience == CliEventAudience(permission="audit:read")
        assert set(event.data) == {"command", "success", "durationMs", "userId"}
        assert event.data["command"] == "echo" and event.data["success"] is True
        assert event.data["userId"] == user_id
        assert "secret-input" not in json.dumps(event.to_message())

        rec.clear()
        res = srv.c.post(BASE + "/execute/stream", headers=_h(root), json={"command": "echo", "value": "x"})
        res.read()
        assert rec.one("command:executed").data["command"] == "echo"

        rec.clear()
        res = srv.c.post(BASE + "/execute/stream", headers=_h(root), json={"command": "ticker"})
        res.read()
        event = rec.one("command:executed")
        assert event.data["command"] == "ticker" and event.data["userId"] == user_id

        rec.clear()
        res = srv.c.post(MCP, headers=_h(root), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": "hi"}},
        })
        assert res.status_code == 200, res.text
        event = rec.one("command:executed")
        assert event.data["command"] == "echo" and event.data["userId"] == user_id

    def test_unknown_commands_and_unprotected_servers(self):
        result = create_cli_server(CliServerOptions(configure=lambda b: b.add_processor(CliEchoCommandProcessor())))
        rec = Recorder(result.event_bus)
        with TestClient(result.app) as client:
            client.post(BASE + "/execute", json={"command": "nope"})
            assert rec.of("command:executed") == []
            client.post(BASE + "/execute", json={"command": "echo", "value": "x"})
            assert rec.one("command:executed").data.keys() == {"command", "success", "durationMs"}


class TestFilesystemEvents:
    def test_api_and_mcp_changes(self, srv: _Server):
        rec = Recorder(srv.result.event_bus)
        root = srv.root
        c = srv.c
        assert c.post("/api/qcli/fs/mkdir", headers=_h(root), json={"path": "/docs"}).status_code == 200
        res = c.post("/api/qcli/fs/upload", headers=_h(root), data={"path": "/docs/a.txt"},
                     files={"file": ("a.txt", b"hello", "text/plain")})
        assert res.status_code == 200, res.text
        res = c.post(MCP, headers=_h(root), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "fs_write", "arguments": {"path": "/docs/b.txt", "content": "hi"}},
        })
        assert res.status_code == 200 and not res.json()["result"].get("isError"), res.text
        assert c.delete("/api/qcli/fs/rm", headers=_h(root), params={"path": "/docs/a.txt"}).status_code == 200

        changes = [(e.data["action"], e.data["path"]) for e in rec.of("fs:changed")]
        assert changes == [("mkdir", "/docs"), ("upload", "/docs/a.txt"), ("write", "/docs/b.txt"),
                           ("delete", "/docs/a.txt")]
        assert all(e.audience == CliEventAudience(permission="files:read") for e in rec.of("fs:changed"))

    async def test_move_and_copy(self):
        bus = CliEventBus()
        rec = Recorder(bus)
        provider = EventPublishingFileStorageProvider(InMemoryFileStorageProvider(), bus)
        await provider.write_file("/a.txt", "x")
        await provider.copy("/a.txt", "/b.txt")
        await provider.move("/b.txt", "/c.txt")
        assert [e.data for e in rec.of("fs:changed")] == [
            {"action": "write", "path": "/a.txt"},
            {"action": "copy", "path": "/a.txt", "newPath": "/b.txt"},
            {"action": "move", "path": "/b.txt", "newPath": "/c.txt"},
        ]
        assert await provider.read_file("/c.txt") == "x"


# --- jobs ----------------------------------------------------------------


class QuickJob(ICliJob):
    async def execute_async(self, context, cancellation_event) -> None:
        return None


class FailingJob(ICliJob):
    async def execute_async(self, context, cancellation_event) -> None:
        raise RuntimeError("broken")


class WaitingJob(ICliJob):
    async def execute_async(self, context, cancellation_event) -> None:
        await cancellation_event.wait()


class SlowJob(ICliJob):
    async def execute_async(self, context, cancellation_event) -> None:
        await asyncio.sleep(5)


async def _until(rec: Recorder, event_type: str, count: int = 1) -> None:
    for _ in range(300):
        if len(rec.of(event_type)) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{event_type} not published: {[e.type for e in rec.events]}")


class TestJobsEvents:
    def _scheduler(self) -> tuple[CliJobScheduler, Recorder]:
        bus = CliEventBus()
        return CliJobScheduler(InMemoryJobStorageProvider(), event_bus=bus), Recorder(bus)

    async def test_run_events(self):
        scheduler, rec = self._scheduler()
        scheduler.register(QuickJob(), CliJobOptions(id="quick"))
        await scheduler.trigger("quick")
        await _until(rec, "job:completed")
        assert [e.type for e in rec.events] == ["job:triggered", "job:started", "job:completed"]
        assert all(e.audience == CliEventAudience(permission="jobs:read") for e in rec.events)
        started, completed = rec.one("job:started"), rec.one("job:completed")
        assert started.data.keys() == {"jobId", "executionId"}
        assert completed.data["executionId"] == started.data["executionId"]
        assert isinstance(completed.data["duration"], int)

    async def test_failed_and_retrying(self):
        scheduler, rec = self._scheduler()
        scheduler.register(FailingJob(), CliJobOptions(id="bad", max_retries=1, retry_delay="0s"))
        await scheduler.trigger("bad")
        await _until(rec, "job:failed", 2)
        assert rec.of("job:failed")[0].data["error"] == "broken"
        retrying = rec.one("job:retrying").data
        assert {k: retrying[k] for k in ("jobId", "attempt", "maxRetries", "delayMs")} == {
            "jobId": "bad", "attempt": 1, "maxRetries": 1, "delayMs": 0,
        }

    async def test_cancelled_and_timed_out(self):
        scheduler, rec = self._scheduler()
        scheduler.register(WaitingJob(), CliJobOptions(id="wait", max_retries=0))
        scheduler.register(SlowJob(), CliJobOptions(id="slow", timeout="1s", max_retries=0))
        await scheduler.trigger("wait")
        await _until(rec, "job:started")
        await scheduler.cancel_current("wait")
        await _until(rec, "job:cancelled")
        assert rec.one("job:cancelled").data["jobId"] == "wait"
        await scheduler.trigger("slow")
        await _until(rec, "job:timed_out")
        assert rec.one("job:timed_out").data["jobId"] == "slow"

    async def test_pause_resume_stop_carry_status(self):
        scheduler, rec = self._scheduler()
        scheduler.register(QuickJob(), CliJobOptions(id="q", interval="1h"))
        await scheduler.pause("q")
        await scheduler.resume("q")
        await scheduler.stop_job("q")
        assert [(e.type, e.data) for e in rec.events] == [
            ("job:paused", {"jobId": "q", "status": "paused"}),
            ("job:resumed", {"jobId": "q", "status": "active"}),
            ("job:stopped", {"jobId": "q", "status": "stopped"}),
        ]

    def test_api_and_mcp_actions(self, srv: _Server):
        jobs = CliJobsBuilder().add_job(QuickJob(), CliJobOptions(id="quick", interval="1h")).build()
        srv.result.mount_plugin(jobs)
        assert jobs.scheduler.event_bus is srv.result.event_bus
        rec = Recorder(srv.result.event_bus)
        root = srv.root
        c = srv.c
        jobs_base = BASE + "/jobs/quick"

        assert c.post(jobs_base + "/trigger", headers=_h(root)).status_code == 200
        for _ in range(300):
            if rec.of("job:completed"):
                break
            c.get(jobs_base, headers=_h(root))
        assert rec.one("job:triggered").data == {"jobId": "quick"}
        execution_id = rec.one("job:completed").data["executionId"]

        assert c.put(jobs_base, headers=_h(root), json={"description": "Quick"}).status_code == 200
        assert rec.one("job:updated").data == {"jobId": "quick"}

        assert c.delete(f"{jobs_base}/history/{execution_id}", headers=_h(root)).status_code == 200
        assert rec.one("job:runs-deleted").data == {"jobId": "quick", "executionId": execution_id}

        # Bulk delete through MCP: one event with the count.
        rec.clear()
        c.post(jobs_base + "/trigger", headers=_h(root))
        for _ in range(300):
            if rec.of("job:completed"):
                break
            c.get(jobs_base, headers=_h(root))
        res = c.post(MCP, headers=_h(root), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "jobs_delete_runs", "arguments": {"id": "quick"}},
        })
        assert res.status_code == 200 and not res.json()["result"].get("isError"), res.text
        assert rec.one("job:runs-deleted").data == {"jobId": "quick", "count": 1}
        # Each action is published once (no double publishing at the API layer).
        assert len(rec.of("job:triggered")) == 1

    async def test_broadcast_fn_still_works_without_a_bus(self):
        received: list[dict] = []

        async def broadcast(message: str) -> None:
            received.append(json.loads(message))

        scheduler = CliJobScheduler(InMemoryJobStorageProvider(), broadcast_fn=broadcast)
        scheduler.register(QuickJob(), CliJobOptions(id="q", interval="1h"))
        await scheduler.pause("q")
        assert received[0]["type"] == "job:paused"
        assert received[0]["jobId"] == "q" and received[0]["status"] == "paused"
        assert "timestamp" in received[0]

    def test_mounting_keeps_a_bus_given_at_build(self):
        own_bus = CliEventBus()
        jobs = CliJobsBuilder().build(event_bus=own_bus)
        create_cli_server().mount_plugin(jobs)
        assert jobs.scheduler.event_bus is own_bus


# =========================================================================
# Socket fan-out: never waits on a client; auth state read on every event
# =========================================================================


class FakeSocket:
    """A connected events socket; ``send_text`` blocks while ``stalled``."""

    class _State:
        name = "CONNECTED"

    client = None
    client_state = _State()

    def __init__(self, stalled: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.release = asyncio.Event()
        if not stalled:
            self.release.set()
        self._closed = asyncio.Event()

    async def send_text(self, text: str) -> None:
        await self.release.wait()
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        await self._closed.wait()
        raise RuntimeError("closed")

    async def close(self, *args: Any, **kwargs: Any) -> None:
        self._closed.set()

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


class _Principal:
    def __init__(self, user_id: str, permissions: list[str]) -> None:
        self.user = type("U", (), {"id": user_id, "name": user_id})()
        self.session = None
        self._permissions = permissions

    def can(self, permission: str) -> bool:
        return permission in self._permissions


async def _connect(manager: Any, socket: FakeSocket, identity: Any = None) -> asyncio.Task:
    task = asyncio.ensure_future(manager.handle_connection(socket, identity))
    await asyncio.sleep(0)
    return task


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestSocketFanOut:
    async def test_publish_does_not_wait_on_a_slow_socket(self):
        result = create_cli_server()
        manager, bus = result.event_socket_manager, result.event_bus
        slow, fast = FakeSocket(stalled=True), FakeSocket()
        tasks = [await _connect(manager, slow), await _connect(manager, fast)]

        for n in range(3):
            await asyncio.wait_for(bus.publish("test:n", {"n": n}), timeout=1)
        await _settle()
        assert fast.types() == ["connected", "test:n", "test:n", "test:n"]
        assert slow.sent == []

        slow.release.set()  # the slow client catches up, in order
        await _settle()
        assert [m.get("n") for m in slow.sent] == [None, 0, 1, 2]
        for socket in (slow, fast):
            await socket.close()
        await asyncio.gather(*tasks)
        assert manager.get_clients() == []

    async def test_authentication_state_is_read_on_every_event(self):
        manager = create_cli_server().event_socket_manager
        bus = CliEventBus()
        manager.attach(bus)
        anonymous, alice = FakeSocket(), FakeSocket()
        tasks = [
            await _connect(manager, anonymous),
            await _connect(manager, alice, _Principal("alice", ["jobs:read"])),
        ]
        required = False
        manager.set_authentication_required(lambda: required)

        await bus.publish("test:open", audience=CliEventAudience(permission="jobs:read"))
        required = True
        await bus.publish("test:scoped", audience=CliEventAudience(permission="jobs:read"))
        await bus.publish("test:everyone")
        await _settle()
        assert anonymous.types() == ["connected", "test:open", "test:everyone"]
        assert alice.types() == ["connected", "test:open", "test:scoped", "test:everyone"]
        for socket in (anonymous, alice):
            await socket.close()
        await asyncio.gather(*tasks)

    def test_users_plugin_marks_the_server_as_requiring_authentication(self):
        result, _ = _protected_server()
        assert result.event_socket_manager._requires_authentication() is True
        assert create_cli_server().event_socket_manager._requires_authentication() is False
