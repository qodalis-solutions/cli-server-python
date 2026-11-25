"""``databases runs [<db>] [-n N] [--json]``: the migration run history through POST /execute, empty history, several
databases, bad limits, unknown databases, and databases:read on a protected server."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_databases import CliDatabasesBuilder, CliMigration, CliMigrationDatabaseOptions, sqlite_connection
from qodalis_cli_databases import runner as runner_module
from qodalis_cli_databases.commands import format_run_line
from qodalis_cli_databases.runner import CliMigrationRun, CliMigrationRunOptions

from .command_helpers import add_users, protect, run_command, sub_permissions

MIGRATIONS = [
    CliMigration("1", "create notes", 'CREATE TABLE "Notes" ("Id" INTEGER PRIMARY KEY)', 'DROP TABLE "Notes"'),
    CliMigration("2", "create tags", 'CREATE TABLE "Tags" ("Id" INTEGER PRIMARY KEY)', 'DROP TABLE "Tags"'),
    CliMigration("3", "broken", 'CREATE TABLE "Tags" ("Id" INTEGER PRIMARY KEY)', "SELECT 1"),
]


def server(names: list[str] | None = None):
    result = create_cli_server()
    builder = CliDatabasesBuilder()
    for name in names or ["main"]:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        builder.add_database(name, CliMigrationDatabaseOptions(sqlite_connection(conn))).add_migrations(name, "app", MIGRATIONS)
    plugin = builder.build()
    result.mount_plugin(plugin)
    return result, plugin


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """Sets the time the runner records (``set("2026-11-25T10:00:00")``)."""
    now = {"value": datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(runner_module, "_now", lambda: now["value"])

    def set_time(text: str) -> None:
        now["value"] = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    return set_time


def run(client: TestClient, plugin, name: str, direction: str, **options) -> None:
    client.portal.call(plugin.databases.run, name, direction, CliMigrationRunOptions(**options))


def test_is_a_sub_command_of_databases_needing_databases_read():
    result, _ = server()
    assert sub_permissions(result, "databases")["runs"] == ["databases:read"]


def test_says_so_when_nothing_ran_yet():
    result, _ = server()
    with TestClient(result.app) as client:
        assert run_command(client, result, "databases runs").text == "main: no migration runs yet"
        payload = json.loads(run_command(client, result, "databases runs main --json").text)
        assert payload == {"database": "main", "items": [], "total": 0}


def test_lists_runs_newest_first_with_the_error_of_a_failed_one(clock):
    result, plugin = server()
    with TestClient(result.app) as client:
        clock("2026-11-25T10:00:00")
        run(client, plugin, "main", "up", to="2", trigger="terminal", actor="alice")
        clock("2026-11-25T10:05:00")
        run(client, plugin, "main", "up", trigger="api")
        out = run_command(client, result, "databases runs main")
        assert out.exit_code == 0
        lines = out.text.split("\n")
        assert lines[0] == "main (sqlite): 2 of 2 run(s)"
        assert re.match(r"^Started\s+Dir\s+Status\s+Count\s+Trigger$", lines[1])
        assert re.match(r"^2026-11-25 10:05:00 up\s+failed\s+0\s+api\s+.*already exists", lines[2])
        assert re.search(r"up\s+succeeded\s+2\s+terminal \(alice\)\s+to 2$", lines[3])


def test_limit_caps_the_list_and_json_prints_the_payload(clock):
    result, plugin = server()
    with TestClient(result.app) as client:
        clock("2026-11-25T10:00:00")
        run(client, plugin, "main", "up", to="1")
        clock("2026-11-25T10:01:00")
        run(client, plugin, "main", "up", to="2")
        clock("2026-11-25T10:02:00")
        run(client, plugin, "main", "down", to="base")
        assert run_command(client, result, "databases runs -n 1").text.split("\n")[0] == "main (sqlite): 1 of 3 run(s)"
        assert len(run_command(client, result, "databases runs --limit 2").text.split("\n")) == 4
        payload = json.loads(run_command(client, result, "databases runs main -n 2 --json").text)
        assert payload["total"] == 3
        assert len(payload["items"]) == 2
        assert payload["items"][0]["direction"] == "down"
        assert payload["items"][0]["status"] == "succeeded"
        assert payload["items"][0]["target"] == "base"


def test_refuses_a_bad_limit():
    result, _ = server()
    with TestClient(result.app) as client:
        for bad in ["0", "101", "abc", "2.5", "-3"]:
            text = run_command(client, result, f"databases runs main --limit {bad}").text
            assert text == "Error: --limit must be a whole number from 1 to 100", bad


def test_needs_the_database_when_there_are_several_and_refuses_an_unknown_one():
    result, _ = server(["main", "logs"])
    with TestClient(result.app) as client:
        assert run_command(client, result, "databases runs").text == "Error: name the database: main, logs"
        assert run_command(client, result, "databases runs nope").text == "Error: unknown database: nope"
        assert run_command(client, result, "databases runs logs").text == "logs: no migration runs yet"


def test_formats_a_run_without_an_actor_target_or_error():
    line = format_run_line(CliMigrationRun(
        id="1", direction="down", trigger="startup", started_at="2026-11-25T10:04:05.123Z", status="running", applied=[],
    ))
    assert line == "2026-11-25 10:04:05 down running       0 startup"


def test_on_a_protected_server_needs_databases_read():
    result, _ = server()
    protect(result)
    with TestClient(result.app) as client:
        tokens = add_users(client, {"runner": ["commands:execute"], "reader": ["commands:execute", "databases:read"]})
        assert run_command(client, result, "databases runs").status == 401
        denied = run_command(client, result, "databases runs", tokens["runner"])
        assert denied.status == 403
        assert "databases:read" in json.dumps(denied.body)
        assert run_command(client, result, "databases runs", tokens["reader"]).text == "main: no migration runs yet"
