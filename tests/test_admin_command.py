"""The admin plugin's ``admin`` terminal command through POST /execute: status (now, --history, --json), plugins
(list, enable, disable: idempotent, unknown, the admin module itself), clients, logs (filters, limits, --since, bad
arguments), and admin:access / logs:read on a protected server."""

from __future__ import annotations

import asyncio
import json
import platform
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_admin import (
    AdminBuildDeps,
    CliAdminBuilder,
    format_duration,
    format_log_line,
    format_table,
    parse_since,
    summarize_status_history,
)
from qodalis_cli_admin.services import system_memory
from qodalis_cli_admin.services.log_ring_buffer import LogEntry
from qodalis_cli_admin.services.time_range import RangeError
from qodalis_cli_server_abstractions import CliCommandProcessor, CliModule

from .command_helpers import add_users, protect, run_command, sub_permissions


class _Forecast(CliCommandProcessor):
    command = "forecast"
    description = "Forecast"

    async def handle_async(self, command, cancellation_event: asyncio.Event | None = None) -> str:  # noqa: ANN001
        return "sunny"


class _Weather(CliModule):
    name = "weather"
    version = "2.0.0"
    description = "Weather"

    def __init__(self) -> None:
        self._processors = [_Forecast()]

    @property
    def processors(self):  # noqa: ANN201
        return self._processors


class Host:
    def __init__(self, users: bool = False) -> None:
        self.result = create_cli_server()
        self.result.builder.add_module(_Weather())
        self.users = protect(self.result) if users else None
        self.admin = CliAdminBuilder().set_credentials("admin", "pass").set_jwt_secret("secret-for-admin-command-tests").build(
            AdminBuildDeps(registry=self.result.registry, event_socket_manager=self.result.event_socket_manager,
                           builder=self.result.builder),
        )
        # Only the entries the tests add
        self.admin.log_buffer.uninstall_handler()
        self.result.mount_plugin(self.admin)
        self.client = TestClient(self.result.app)
        self.tokens: dict[str, str] = {}

    def run(self, line: str, user: str | None = None):
        return run_command(self.client, self.result, line, self.tokens[user] if user else None)

    def text(self, line: str, user: str | None = None) -> str:
        return self.run(line, user).text

    def sample(self, minutes_ago: float, memory: float, cpu: float | None, ws: int, shell: int) -> None:
        self.admin.status_history.samples.append({
            "timestamp": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            "memoryMb": memory, "cpuPercent": cpu, "activeWsConnections": ws, "activeShellSessions": shell,
        })


@pytest.fixture
def host():
    h = Host()
    yield h
    h.admin.log_buffer.uninstall_handler()


# --------------------------------------------------------------------------- registration


def test_is_the_admin_module_with_its_sub_commands_and_permissions(host: Host):
    root = host.result.registry.find_processor("admin")
    assert root.required_permissions == ["admin:access"]
    assert sub_permissions(host.result, "admin") == {"status": None, "plugins": None, "clients": None, "logs": ["logs:read"], "dashboard": None}
    assert any(m.name == "admin" for m in host.result.builder.modules)
    plugins = next(p for p in root.processors if p.command == "plugins")
    assert [p.command for p in plugins.processors] == ["list", "enable", "disable"]


# --------------------------------------------------------------------------- status


def test_status_prints_version_platform_uptime_memory_cpu_connections(host: Host):
    out = host.run("admin status")
    assert out.exit_code == 0
    lines = out.text.split("\n")
    assert re.match(r"^Server {6}qodalis-cli-server \S+", lines[0])
    assert lines[1] == f"Platform    python {platform.python_version()} ({platform.system()} {platform.release()})"
    assert re.match(r"^Uptime {6}\d+ (s|min|h \d+ min|d \d+ h) \(since \d{4}-\d\d-\d\d \d\d:\d\d:\d\d\)$", lines[2])
    assert re.match(r"^Memory {6}[\d.]+ MB( \(process\) · (machine|container) .+)?$", lines[3])
    assert lines[4] == "CPU         n/a"
    assert lines[5] == "Connections 0 WebSocket, 0 shell"
    assert re.match(r"^Commands {4}\d+$", lines[6])
    assert lines[7] == "Jobs        0"
    assert lines[8] == "Features    none"
    # admin alone is the status
    assert host.text("admin").split("\n")[0] == lines[0]


def test_status_json_prints_the_get_status_payload_with_the_server_package(host: Host):
    body = json.loads(host.text("admin status --json"))
    assert body["platform"] == "python"
    assert body["server"]["id"] == "qodalis-cli-server"
    assert body["cpuUsagePercent"] is None
    assert isinstance(body["memoryUsageMb"], (int, float))
    assert {"systemMemoryTotalMb", "systemMemoryUsedMb", "memoryLimited"} <= body.keys()


def test_status_memory_the_machine_use_or_the_container_limit_and_its_usage(host: Host, monkeypatch: pytest.MonkeyPatch):
    readers = system_memory.default_readers
    gb = 1024 ** 3
    monkeypatch.setattr(readers, "total_bytes", lambda: 16 * gb)
    monkeypatch.setattr(readers, "available_bytes", lambda: 6 * gb)
    monkeypatch.setattr(readers, "limit_bytes", lambda: None)
    assert re.search(r"^Memory {6}[\d.]+ MB \(process\) · machine 10 GB of 16 GB used \(63%\)$", host.text("admin status"), re.M)
    body = json.loads(host.text("admin status --json"))
    assert (body["systemMemoryTotalMb"], body["systemMemoryUsedMb"], body["memoryLimited"]) == (16384, 10240, False)

    monkeypatch.setattr(readers, "limit_bytes", lambda: 512 * 1024 ** 2)
    monkeypatch.setattr(readers, "cgroup_usage_bytes", lambda: 300 * 1024 ** 2)
    assert re.search(r"^Memory {6}[\d.]+ MB \(process\) · container 300 MB of 512 MB used \(59%\)$", host.text("admin status"), re.M)
    body = json.loads(host.text("admin status --json"))
    assert (body["systemMemoryTotalMb"], body["systemMemoryUsedMb"], body["memoryLimited"]) == (512, 300, True)

    def unreadable() -> float:
        raise OSError("unreadable")

    monkeypatch.setattr(readers, "total_bytes", unreadable)
    assert re.search(r"^Memory {6}[\d.]+ MB$", host.text("admin status"), re.M)
    body = json.loads(host.text("admin status --json"))
    assert (body["systemMemoryTotalMb"], body["systemMemoryUsedMb"], body["memoryLimited"]) == (None, None, None)


def test_status_shows_the_latest_cpu_sample(host: Host):
    host.sample(0, 10, 3.5, 0, 0)
    assert "CPU         3.5%" in host.text("admin status")


def test_status_history_summary(host: Host):
    host.sample(120, 999, 99, 9, 9)
    host.sample(30, 100, None, 1, 0)
    host.sample(20, 110, 2, 3, 1)
    host.sample(10, 120, 4, 2, 0)
    lines = host.text("admin status --history").split("\n")
    assert re.match(r"^Last 60 min: 3 sample\(s\) every 15 s, \S+ \S+ to \S+ \S+$", lines[0])
    assert lines[1] == "Memory      min 100 MB  avg 110 MB  max 120 MB"
    assert lines[2] == "CPU         min 2%  avg 3%  max 4%"
    assert lines[3] == "Peak        3 WebSocket, 1 shell"
    assert "4 sample(s)" in host.text("admin status --history --minutes 180")
    body = json.loads(host.text("admin status --history --minutes 15 --json"))
    assert body["minutes"] == 15 and body["intervalSeconds"] == 15 and body["count"] == 1
    assert body["memoryMb"] == {"min": 120, "avg": 120, "max": 120}


def test_status_history_without_samples_and_cpu_na(host: Host):
    assert host.text("admin status --history") == "No status samples in the last 60 min."
    host.sample(0, 50, None, 0, 0)
    assert "CPU         n/a" in host.text("admin status --history")


def test_status_refuses_bad_minutes_and_minutes_without_history(host: Host):
    for bad in ["0", "1441", "x", "1.5"]:
        assert host.text(f"admin status --history --minutes {bad}") == "Error: --minutes must be a whole number from 1 to 1440"
    assert host.text("admin status --minutes 5") == "Error: --minutes goes with --history"


# --------------------------------------------------------------------------- plugins


def test_plugins_lists_the_modules_in_columns_and_as_json(host: Host):
    text = host.text("admin plugins")
    lines = text.split("\n")
    assert re.match(r"^ID\s+Name\s+Version\s+Status\s+Commands$", lines[0])
    assert re.match(r"^weather\s+weather\s+2\.0\.0\s+enabled\s+1$", next(l for l in lines if l.startswith("weather")))
    assert re.match(r"^admin\s+admin\s+\S+\s+enabled\s+1$", next(l for l in lines if l.startswith("admin")))
    assert host.text("admin plugins list") == text
    modules = json.loads(host.text("admin plugins --json"))
    weather = next(m for m in modules if m["id"] == "weather")
    assert weather["enabled"] is True and weather["processors"] == ["forecast"]


def test_disable_blocks_enable_brings_back_idempotent_case_insensitive(host: Host):
    assert host.text("forecast") == "sunny"
    assert host.text("admin plugins disable Weather") == "Disabled weather (1 command)."
    assert host.text("admin plugins disable weather") == "weather is already disabled."
    assert re.search(r"weather\s+weather\s+2\.0\.0\s+disabled", host.text("admin plugins"))
    blocked = host.run("forecast")
    assert blocked.exit_code == 1
    assert "disabled" in blocked.text
    assert host.text("admin plugins enable weather") == "Enabled weather (1 command)."
    assert host.text("admin plugins enable weather") == "weather is already enabled."
    assert host.text("forecast") == "sunny"


def test_the_same_state_as_the_dashboard_toggle(host: Host):
    host.text("admin plugins disable weather")
    token = host.client.post("/api/v1/qcli/auth/login", json={"username": "admin", "password": "pass"}).json()["token"]
    res = host.client.get("/api/v1/qcli/plugins/weather", headers={"Authorization": f"Bearer {token}"})
    assert res.json()["enabled"] is False
    assert len(host.admin.module.processors) == 1


def test_plugins_refuses_unknown_missing_extra_and_the_admin_module(host: Host):
    assert host.text("admin plugins enable nope") == "Error: unknown module: nope (admin plugins lists them)"
    assert host.text("admin plugins disable") == "Error: name one module: admin plugins disable <id> (admin plugins lists them)"
    assert host.text("admin plugins enable a b") == "Error: name one module: admin plugins enable <id> (admin plugins lists them)"
    assert host.text("admin plugins disable admin") == (
        "Error: the admin module cannot be disabled from the terminal: it would disable this command"
    )
    # enabling it is harmless
    assert host.text("admin plugins enable admin") == "admin is already enabled."


# --------------------------------------------------------------------------- clients


def test_clients_says_so_when_nobody_is_connected(host: Host):
    assert host.text("admin clients") == "No WebSocket clients connected."
    assert host.text("admin clients --json") == "[]"


def test_clients_lists_the_connected_clients(host: Host, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(host.result.event_socket_manager, "get_clients", lambda: [
        {"id": "c1", "connectedAt": "2026-11-25T10:00:00.000Z", "remoteAddress": "127.0.0.1", "type": "events"},
        {"id": "client-2", "connectedAt": "2026-11-25T11:30:15.500Z", "remoteAddress": "", "type": "events"},
    ])
    assert host.text("admin clients") == "\n".join([
        "2 client(s)",
        "ID        Type    Connected            Address",
        "c1        events  2026-11-25 10:00:00  127.0.0.1",
        "client-2  events  2026-11-25 11:30:15  -",
    ])
    assert len(json.loads(host.text("admin clients --json"))) == 2


# --------------------------------------------------------------------------- logs


@pytest.fixture
def logs(host: Host) -> Host:
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    for minutes, level, source, message in [
        (0, "INFO", "server", "started"),
        (60, "DEBUG", "jobs", "tick"),
        (120, "WARN", "jobs", "slow job cleanup"),
        (170, "ERROR", "db", "connection timeout"),
        (175, "INFO", "server", "request served"),
    ]:
        stamp = (base + timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        host.admin.log_buffer.add(LogEntry(stamp, level, message, source))
    return host


def test_logs_prints_the_latest_entries_oldest_first(logs: Host):
    lines = logs.text("admin logs").split("\n")
    assert len(lines) == 5
    assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO  \[server\] started$", lines[0])
    assert re.search(r"ERROR \[db\] connection timeout$", lines[3])


def test_logs_limit_keeps_the_newest_and_says_how_many(logs: Host):
    lines = logs.text("admin logs -n 2").split("\n")
    assert lines[0] == "Last 2 of 5 entries"
    assert "connection timeout" in lines[1]
    assert "request served" in lines[2]
    assert len(logs.text("admin logs --limit 5").split("\n")) == 5


def test_logs_filters_by_level_search_service_and_since(logs: Host):
    assert len(logs.text("admin logs --level warn,error").split("\n")) == 2
    assert "slow job cleanup" in logs.text("admin logs --level WARNING")
    assert "connection timeout" in logs.text("admin logs --search TIMEOUT")
    assert len(logs.text("admin logs --service jobs").split("\n")) == 2
    assert len(logs.text("admin logs --since 30m").split("\n")) == 2
    iso = (datetime.now(timezone.utc) - timedelta(minutes=65)).isoformat().replace("+00:00", "Z")
    assert len(logs.text(f"admin logs --since {iso}").split("\n")) == 3
    assert logs.text("admin logs --search nothing-like-this") == "No log entries match."


def test_logs_json_prints_the_entries_and_the_total(logs: Host):
    body = json.loads(logs.text("admin logs -n 1 --json"))
    assert body["total"] == 5
    assert len(body["entries"]) == 1
    assert body["entries"][0]["level"] == "INFO" and body["entries"][0]["message"] == "request served"


def test_logs_says_so_when_the_buffer_is_empty(host: Host):
    assert host.text("admin logs") == "No log entries match."


def test_logs_refuses_bad_arguments(logs: Host):
    assert logs.text("admin logs --level fatal,info") == "Error: unknown level fatal (debug, info, warn, error)"
    assert logs.text("admin logs -n 0") == "Error: --limit must be a whole number from 1 to 1000"
    assert logs.text("admin logs --limit 1001") == "Error: --limit must be a whole number from 1 to 1000"
    assert logs.text("admin logs --since yesterday") == "Error: --since must be a duration (30s, 5m, 1h, 7d) or an ISO 8601 date"
    assert logs.text("admin logs --since 0m") == "Error: --since must be more than 0"
    assert logs.text("admin logs --since 2999-01-01") == "Error: --since is in the future"
    assert logs.text("admin logs --search") == "Error: --search needs a value"


# --------------------------------------------------------------------------- protected server


def test_protected_server_needs_admin_access_and_logs_read_for_logs():
    h = Host(users=True)
    try:
        with h.client:
            h.tokens = add_users(h.client, {
                "runner": ["commands:execute"],
                "admin": ["commands:execute", "admin:access"],
                "logger": ["commands:execute", "admin:access", "logs:read"],
            })
            h.admin.log_buffer.add(LogEntry(datetime.now(timezone.utc).isoformat(), "INFO", "hello", "x"))
            assert h.run("admin status").status == 401
            denied = h.run("admin status", "runner")
            assert denied.status == 403
            assert "admin:access" in json.dumps(denied.body)
            assert h.run("admin plugins disable weather", "runner").status == 403
            assert "Platform" in h.text("admin status", "admin")
            assert "weather" in h.text("admin plugins", "admin")
            no_logs = h.run("admin logs", "admin")
            assert no_logs.status == 403
            assert "logs:read" in json.dumps(no_logs.body)
            assert "hello" in h.text("admin logs", "logger")
            assert h.text("admin clients", "root") == "No WebSocket clients connected."
    finally:
        h.admin.log_buffer.uninstall_handler()


# --------------------------------------------------------------------------- helpers


def test_format_duration():
    assert format_duration(0) == "0 s"
    assert format_duration(59) == "59 s"
    assert format_duration(60) == "1 min"
    assert format_duration(7384) == "2 h 3 min"
    assert format_duration(90_000 + 3 * 3600) == "1 d 4 h"


def test_format_table_pads_every_column_but_the_last():
    assert format_table(["A", "Bee"], [["xx", "y"], ["z", ""]]) == "A   Bee\nxx  y\nz"


def test_format_log_line_without_a_source():
    entry: dict[str, Any] = {"timestamp": "2026-11-25T10:00:00.000Z", "level": "WARN", "source": "", "message": "m"}
    assert format_log_line(entry) == "2026-11-25 10:00:00 WARN  m"


def test_summarize_status_history_of_nothing():
    assert summarize_status_history([]) == {"count": 0, "maxWsConnections": 0, "maxShellSessions": 0}


def test_summarize_status_history_keeps_two_decimals():
    def sample(memory: float) -> dict[str, Any]:
        return {"timestamp": "2026-11-25T10:00:00.000Z", "memoryMb": memory, "cpuPercent": None,
                "activeWsConnections": 0, "activeShellSessions": 0}

    assert summarize_status_history([sample(213.95), sample(213.95), sample(214)])["memoryMb"] == {
        "min": 213.95, "avg": 213.97, "max": 214,
    }


def test_parse_since():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    assert parse_since("30s", "s", now) == datetime(2026, 11, 25, 11, 59, 30, tzinfo=timezone.utc)
    assert parse_since("2H", "s", now) == datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    assert parse_since("7d", "s", now) == datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert parse_since("2026-11-25T08:00", "s", now) == datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(RangeError):
        parse_since("5w", "s", now)
