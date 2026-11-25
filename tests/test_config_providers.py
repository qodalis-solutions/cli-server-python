"""The built-in config providers and their runtime effect: server, host
(secrets masked), cors (live origins: CORS headers, WebSocket upgrades, the
MCP endpoint), mcp (tool lists, read-only), filesystem (upload limit), jobs
(history retention), logs (buffer), users (sessions, passwords), custom;
persistence through the stores.  Port of the Node server's
``config-providers.test.ts`` (plus the plugin providers Python ships)."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sqlite3
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.config import CliHostConfigProvider, is_secret_environment_variable
from qodalis_cli.config.providers.host_config_provider import (
    DEFAULT_ENVIRONMENT_ALLOW,
    CliHostEnvironmentOptions,
    CliHostOption,
)
from qodalis_cli.create_cli_server import CliServerConfigOptions
from qodalis_cli.filesystem.filesystem_options import FileSystemOptions
from qodalis_cli.mcp import CliMcpOptions
from qodalis_cli_admin.services.admin_config_providers import create_logs_config_provider
from qodalis_cli_admin.services.log_ring_buffer import LogEntry, LogRingBuffer
from qodalis_cli_databases import CliDatabaseConfigStore, sqlite_connection
from qodalis_cli_filesystem.providers.in_memory_provider import InMemoryFileStorageProvider
from qodalis_cli_jobs import PRUNE_HISTORY_JOB_ID, CliJobsBuilder
from qodalis_cli_server_abstractions.config import CliConfigError, JsonFileConfigStore, cli_config_context
from qodalis_cli_server_abstractions.jobs import JobHistoryRetention
from qodalis_cli_users import CliUsersBuilder

ALL = cli_config_context(None, "api")
MCP = "/api/qcli/mcp"
VERSION = "/api/v1/qcli/version"


def section(view: dict[str, Any], section_id: str) -> dict[str, Any]:
    found = next((s for s in view["sections"] if s["id"] == section_id), None)
    if found is None:
        raise AssertionError(f"no section {section_id}")
    return found


def value_of(s: dict[str, Any], key: str) -> Any:
    return next((e["value"] for e in s["entries"] if e["key"] == key), None)


def entry_of(s: dict[str, Any], key: str) -> dict[str, Any]:
    return next(e for e in s["entries"] if e["key"] == key)


async def refused(awaitable: Any) -> CliConfigError:
    with pytest.raises(CliConfigError) as info:
        await awaitable
    return info.value


def client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def acao(app: Any, origin: str, path: str = VERSION) -> str | None:
    async with client(app) as c:
        res = await c.get(path, headers={"Origin": origin})
    return res.headers.get("access-control-allow-origin")


async def mcp_call(app: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with client(app) as c:
        res = await c.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return res.json()


async def tool_names(app: Any) -> list[str]:
    return [t["name"] for t in (await mcp_call(app, "tools/list"))["result"]["tools"]]


# ---------------------------------------------------------------------------
# built-in providers
# ---------------------------------------------------------------------------


class TestBuiltIn:
    async def test_registered_on_every_server_in_order_filesystem_only_with_a_filesystem(self) -> None:
        plain = create_cli_server(CliServerOptions())
        assert [p.id for p in plain.builder.config_providers] == ["server", "host", "cors", "mcp", "custom"]
        with_fs = create_cli_server(CliServerOptions(
            configure=lambda b: b.set_file_storage_provider(InMemoryFileStorageProvider()),
        ))
        assert "filesystem" in [p.id for p in with_fs.builder.config_providers]
        assert plain.config is plain.builder.config

    async def test_a_host_provider_with_a_built_in_id_replaces_the_built_in(self) -> None:
        class Mine:
            id = "custom"
            title = "Mine"

            def get_sections(self, context: Any = None) -> list:
                return []

        cli = create_cli_server(CliServerOptions(configure=lambda b: b.add_config_provider(Mine())))
        assert (await cli.config.get("custom", ALL))["title"] == "Mine"

    async def test_server_the_process_read_only(self) -> None:
        view = await create_cli_server(CliServerOptions()).config.get("server", ALL)
        assert view["writable"] is False
        runtime = section(view, "runtime")
        assert value_of(runtime, "platform") == "python"
        assert value_of(runtime, "platformVersion") == sys.version.split()[0]
        assert value_of(runtime, "pid") == os.getpid()
        assert re.match(r"^\d+(ms|s|m|h|d)$", value_of(runtime, "uptime"))
        # Node's ``nodeEnv``
        assert isinstance(value_of(runtime, "environment"), str)
        assert all(not e["mutable"] for e in runtime["entries"])


# ---------------------------------------------------------------------------
# host
# ---------------------------------------------------------------------------


def _env_entry(e: Any) -> dict[str, Any]:
    """A raw provider entry as the Node test compares it (fields that are set)."""
    out: dict[str, Any] = {"key": e.key, "type": e.type, "value": e.value}
    if e.is_set is not None:
        out["isSet"] = e.is_set
    if e.sensitive:
        out["sensitive"] = True
    if e.source is not None:
        out["source"] = e.source
    return out


class TestHost:
    def test_names_and_urls_that_hold_secrets(self) -> None:
        for name in [
            "DB_PASSWORD", "JWT_SECRET", "GITHUB_TOKEN", "API_KEY", "STRIPE_APIKEY", "KEY", "AWS_SECRET_ACCESS_KEY",
            "PRIVATE_KEY_PATH", "SESSION_COOKIE", "SENTRY_DSN", "CONNECTION_STRING", "QCLI_ADMIN_PASSWORD", "BASIC_AUTH",
            "TERM_SESSION_ID", "SSH_PASSPHRASE", "DB_PASS", "MONGO_CONN", "REDIS_CONNSTR", "GPG_KEYRING",
            "ssh_auth_sock", "Oauth_Client", "X_CREDENTIALS",
        ]:
            assert is_secret_environment_variable(name, "x") is True, name
        for name in ["PATH", "HOME", "PYTHON_ENV", "ENVIRONMENT", "PORT", "PWD", "HOST", "TZ", "LANG", "QCLI_PORT"]:
            assert is_secret_environment_variable(name, "x") is False, name
        assert is_secret_environment_variable("DATABASE_URL", "postgres://user:pw@db:5432/app") is True
        assert is_secret_environment_variable("DATABASE_URL", "postgres://db:5432/app") is False
        assert is_secret_environment_variable("UPSTREAM", "https://admin:hunter2@example.com/x") is True
        assert is_secret_environment_variable("UPSTREAM", "https://user@example.com/x") is False
        assert is_secret_environment_variable("MY_THING", "x", ["MY_*"]) is True

    def test_shows_only_qcli_and_a_few_harmless_variables_by_default_allow_adds_names(self) -> None:
        assert list(DEFAULT_ENVIRONMENT_ALLOW) == ["QCLI_*", "PYTHON_ENV", "ENVIRONMENT", "PORT", "HOST", "TZ", "LANG"]
        env = {
            "PATH": "/usr/bin", "HOME": "/home/x", "TERM_SESSION_ID": "abc", "SSH_AUTH_SOCK": "/tmp/ssh",
            "AWS_ACCESS_KEY_ID": "AKIA", "PYTHON_ENV": "production", "PORT": "8048", "HOST": "0.0.0.0", "TZ": "UTC",
            "LANG": "en_US.UTF-8", "QCLI_URL": "https://a:b@x.io", "QCLI_MODE": "demo", "APP_REGION": "eu",
        }

        def keys(options: CliHostEnvironmentOptions) -> list[str]:
            return [e.key for e in CliHostConfigProvider(lambda: [], options, env).get_sections()[1].entries]

        assert keys(CliHostEnvironmentOptions()) == ["HOST", "LANG", "PORT", "PYTHON_ENV", "QCLI_MODE", "QCLI_URL", "TZ"]
        assert keys(CliHostEnvironmentOptions(allow=["APP_*", "TERM_SESSION_ID"])) == [
            "APP_REGION", "HOST", "LANG", "PORT", "PYTHON_ENV", "QCLI_MODE", "QCLI_URL", "TERM_SESSION_ID", "TZ",
        ]
        entries = CliHostConfigProvider(
            lambda: [], CliHostEnvironmentOptions(allow=["TERM_SESSION_ID"]), env,
        ).get_sections()[1].entries
        term = next(e for e in entries if e.key == "TERM_SESSION_ID")
        assert (term.type, term.value, term.is_set) == ("secret", None, True)
        url = next(e for e in entries if e.key == "QCLI_URL")
        assert (url.type, url.value) == ("secret", None)
        assert next(e for e in entries if e.key == "QCLI_MODE").value == "demo"
        dumped = json.dumps([_env_entry(e) for e in entries])
        assert not re.search(r"/usr/bin|abc|AKIA|a:b@", dumped)

    def test_lists_the_environment_sorted_masks_secrets_include_exclude(self) -> None:
        # Node's npm_config_x (left out there) has no Python counterpart
        env = {
            "ZED": "z", "ALPHA": "a", "DB_PASSWORD": "hunter2", "EMPTY_TOKEN": "",
            "DATABASE_URL": "mysql://u:p@h/db", "QCLI_PORT": "8047",
        }
        host = CliHostConfigProvider(
            lambda: [CliHostOption("basePath", "/api/qcli")], CliHostEnvironmentOptions(include=["*"]), env,
        )
        options, environment = host.get_sections()
        assert [(e.key, e.type, e.value) for e in options.entries] == [("basePath", "string", "/api/qcli")]
        assert [e.key for e in environment.entries] == [
            "ALPHA", "DATABASE_URL", "DB_PASSWORD", "EMPTY_TOKEN", "QCLI_PORT", "ZED",
        ]
        by_key = {e.key: e for e in environment.entries}
        assert _env_entry(by_key["DB_PASSWORD"]) == {
            "key": "DB_PASSWORD", "type": "secret", "value": None, "isSet": True, "sensitive": True,
            "source": "environment",
        }
        assert by_key["EMPTY_TOKEN"].is_set is False
        assert (by_key["DATABASE_URL"].type, by_key["DATABASE_URL"].value) == ("secret", None)
        assert _env_entry(by_key["ALPHA"]) == {"key": "ALPHA", "type": "string", "value": "a", "source": "environment"}
        dumped = json.dumps([_env_entry(e) for e in environment.entries])
        assert "hunter2" not in dumped and "u:p@h" not in dumped

        only = CliHostConfigProvider(
            lambda: [],
            CliHostEnvironmentOptions(include=["QCLI_*", "ZED"], exclude=["ZED"], secrets=["QCLI_PORT"]),
            env,
        )
        assert [_env_entry(e) for e in only.get_sections()[1].entries] == [{
            "key": "QCLI_PORT", "type": "secret", "value": None, "isSet": True, "sensitive": True,
            "source": "environment",
        }]

    async def test_through_the_server_options_typed_secrets_never_in_the_response(self, monkeypatch) -> None:
        monkeypatch.setenv("QCLI_TEST_SECRET_TOKEN", "do-not-show-me")
        cli = create_cli_server(CliServerOptions(
            base_path="/api/custom", max_table_rows=50,
            config=CliServerConfigOptions(environment=CliHostEnvironmentOptions(include=["QCLI_TEST_*"])),
        ))
        view = await cli.config.get("host", ALL)
        options = section(view, "options")
        assert value_of(options, "basePath") == "/api/custom"
        rows = entry_of(options, "maxTableRows")
        assert (rows["type"], rows["value"]) == ("number", 50)
        shell = entry_of(options, "enableShell")
        assert (shell["type"], shell["value"]) == ("boolean", False)
        environment = section(view, "environment")["entries"]
        assert len(environment) == 1
        assert {k: environment[0][k] for k in ("key", "value", "isSet", "sensitive")} == {
            "key": "QCLI_TEST_SECRET_TOKEN", "value": None, "isSet": True, "sensitive": True,
        }
        assert "do-not-show-me" not in json.dumps(view)

    async def test_the_default_server_shows_qcli_variables_only(self, monkeypatch) -> None:
        monkeypatch.setenv("QCLI_TEST_VISIBLE", "yes")
        monkeypatch.setenv("SOME_APP_SETTING", "hidden")
        view = await create_cli_server(CliServerOptions()).config.get("host", ALL)
        keys = [e["key"] for e in section(view, "environment")["entries"]]
        assert "QCLI_TEST_VISIBLE" in keys
        assert "SOME_APP_SETTING" not in keys
        assert "PATH" not in keys


# ---------------------------------------------------------------------------
# cors
# ---------------------------------------------------------------------------

ORIGIN = "https://new.example"


class TestCors:
    async def test_a_new_origin_gets_cors_headers_right_away_reset_restores_the_start_list(self) -> None:
        cli = create_cli_server(CliServerOptions(allowed_origins=["https://old.example"]))
        cors = section(await cli.config.get("cors", ALL), "cors")
        assert value_of(cors, "mode") == "origins"
        assert value_of(cors, "allowedOrigins") == ["https://old.example"]
        allowed = entry_of(cors, "allowedOrigins")
        assert (allowed["type"], allowed["editor"], allowed["mutable"]) == ("string[]", "origins", True)

        assert await acao(cli.app, ORIGIN) is None
        await cli.config.update("cors", "cors", {"changes": {"allowedOrigins": ["https://old.example", ORIGIN]}}, ALL)
        assert await acao(cli.app, ORIGIN) == ORIGIN
        assert cli.allowed_origins == ["https://old.example", ORIGIN]

        await cli.config.update("cors", "cors", {"changes": {"allowedOrigins": None}}, ALL)
        assert await acao(cli.app, ORIGIN) is None
        assert await acao(cli.app, "https://old.example") == "https://old.example"

    async def test_a_server_started_without_origins_can_add_some(self) -> None:
        cli = create_cli_server(CliServerOptions())
        assert await acao(cli.app, ORIGIN) is None
        await cli.config.update("cors", "cors", {"changes": {"allowedOrigins": [ORIGIN]}}, ALL)
        assert await acao(cli.app, ORIGIN) == ORIGIN

    async def test_the_mcp_endpoint_follows_the_live_list(self) -> None:
        cli = create_cli_server(CliServerOptions(mcp=True, allowed_origins=[]))
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        async with client(cli.app) as c:
            assert (await c.post(MCP, json=ping, headers={"Origin": ORIGIN})).status_code == 403
            await cli.config.update("cors", "cors", {"changes": {"allowedOrigins": [ORIGIN]}}, ALL)
            res = await c.post(MCP, json=ping, headers={"Origin": ORIGIN})
            assert res.status_code == 200, res.text

    def test_websocket_upgrades_follow_the_live_list(self) -> None:
        cli = create_cli_server(CliServerOptions(allowed_origins=["https://old.example"]))
        with TestClient(cli.app) as c:
            with c.websocket_connect("/ws/v1/qcli/events", headers={"origin": ORIGIN}) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
            assert exc.value.code == 4403
            asyncio.run(cli.config.update("cors", "cors", {"changes": {"allowedOrigins": [ORIGIN]}}, ALL))
            with c.websocket_connect("/ws/v1/qcli/events", headers={"origin": ORIGIN}) as ws:
                assert json.loads(ws.receive_text())["type"] == "connected"

    async def test_validates_each_origin(self) -> None:
        cli = create_cli_server(CliServerOptions())
        for bad in ["example.com", "https://a.example/path", "ftp://a.example", "*", "https://a.example:99999"]:
            err = await refused(cli.config.update("cors", "cors", {"changes": {"allowedOrigins": [bad]}}, ALL))
            assert err.status == 400, bad
            assert "is not an origin" in err.errors["allowedOrigins"]
        await cli.config.update(
            "cors", "cors",
            {"changes": {"allowedOrigins": ["http://localhost:*", "https://a.example:8443", "http://h/"]}}, ALL,
        )

    async def test_the_mode_reflects_the_cors_option_and_is_read_only(self) -> None:
        # Node's ``cors: {origin: true}`` (custom) is ``cors_origins`` here
        modes = [
            (CliServerOptions(cors=True), "permissive"),
            (CliServerOptions(cors=False), "disabled"),
            (CliServerOptions(cors_origins=["https://x.example"]), "custom"),
            (CliServerOptions(), "origins"),
        ]
        for options, mode in modes:
            cli = create_cli_server(options)
            assert value_of(section(await cli.config.get("cors", ALL), "cors"), "mode") == mode
        cli = create_cli_server(CliServerOptions())
        err = await refused(cli.config.update("cors", "cors", {"changes": {"mode": "permissive"}}, ALL))
        assert err.errors == {"mode": "read-only"}

    async def test_never_changes_the_hosts_own_list(self) -> None:
        origins = ["https://a.example"]
        cli = create_cli_server(CliServerOptions(allowed_origins=origins))
        await cli.config.update("cors", "cors", {"changes": {"allowedOrigins": ["https://b.example"]}}, ALL)
        assert origins == ["https://a.example"]


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


class TestMcp:
    async def test_read_only_hides_config_set_and_a_call_is_refused(self) -> None:
        cli = create_cli_server(CliServerOptions(mcp=True))
        assert {"config_list", "config_get", "config_set"} <= set(await tool_names(cli.app))
        await cli.config.update("mcp", "mcp", {"changes": {"readOnly": True}}, ALL)
        listed = await tool_names(cli.app)
        assert {"config_list", "config_get", "server_info"} <= set(listed)
        assert "config_set" not in listed
        call = await mcp_call(cli.app, "tools/call", {
            "name": "config_set",
            "arguments": {"provider": "custom", "section": "custom", "changes": {"a": 1}},
        })
        assert call["error"] == {
            "code": -32602, "message": "Tool config_set is not available: the MCP endpoint is read-only",
        }
        assert (await cli.config.get("custom", ALL))["sections"][0]["entries"] == []
        await cli.config.update("mcp", "mcp", {"changes": {"readOnly": None}}, ALL)
        assert "config_set" in await tool_names(cli.app)

    async def test_allow_list_and_deny_list_empty_allows_every_tool(self) -> None:
        cli = create_cli_server(CliServerOptions(mcp=CliMcpOptions(exclude=["server_info"])))
        assert "server_info" not in await tool_names(cli.app)
        await cli.config.update("mcp", "mcp", {"changes": {"tools": ["config_*"], "exclude": []}}, ALL)
        assert sorted(await tool_names(cli.app)) == ["config_get", "config_list", "config_set"]
        await cli.config.update("mcp", "mcp", {"changes": {"tools": []}}, ALL)
        assert "server_info" in await tool_names(cli.app)
        await cli.config.update("mcp", "mcp", {"changes": {"exclude": None}}, ALL)
        assert "server_info" not in await tool_names(cli.app)

    async def test_instructions_are_returned_by_initialize(self) -> None:
        cli = create_cli_server(CliServerOptions(mcp=CliMcpOptions(instructions="first")))

        async def init() -> Any:
            return (await mcp_call(cli.app, "initialize", {"protocolVersion": "2025-06-18"}))["result"].get("instructions")

        assert await init() == "first"
        await cli.config.update("mcp", "mcp", {"changes": {"instructions": "second"}}, ALL)
        assert await init() == "second"
        await cli.config.update("mcp", "mcp", {"changes": {"instructions": ""}}, ALL)
        assert await init() is None

    async def test_validates_tool_patterns_read_only_when_mcp_is_off(self) -> None:
        on = create_cli_server(CliServerOptions(mcp=True))
        assert (await refused(on.config.update("mcp", "mcp", {"changes": {"tools": ["Bad Name"]}}, ALL))).status == 400
        off = create_cli_server(CliServerOptions())
        view = section(await off.config.get("mcp", ALL), "mcp")
        assert value_of(view, "enabled") is False
        assert view["mutable"] is False
        assert (await refused(off.config.update("mcp", "mcp", {"changes": {"readOnly": True}}, ALL))).status == 400

    async def test_never_changes_the_hosts_mcp_object(self) -> None:
        options = CliMcpOptions(tools=["a_*"])
        cli = create_cli_server(CliServerOptions(mcp=options))
        await cli.config.update("mcp", "mcp", {"changes": {"tools": ["b_*"], "readOnly": True}}, ALL)
        assert options.tools == ["a_*"]
        assert options.read_only is False
        as_dict = {"tools": ["a_*"]}
        cli = create_cli_server(CliServerOptions(mcp=as_dict))
        await cli.config.update("mcp", "mcp", {"changes": {"tools": ["b_*"], "readOnly": True}}, ALL)
        assert as_dict == {"tools": ["a_*"]}


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------


class TestFilesystem:
    async def test_a_lower_limit_refuses_larger_uploads_with_413_storage_and_roots_read_only(self) -> None:
        cli = create_cli_server(CliServerOptions(configure=lambda b: (
            b.set_file_storage_provider(InMemoryFileStorageProvider()),
            b.add_filesystem(FileSystemOptions(max_upload_bytes=1000)),
        )))
        view = section(await cli.config.get("filesystem", ALL), "filesystem")
        assert value_of(view, "maxUploadBytes") == 1000
        assert entry_of(view, "allowedPaths")["mutable"] is False
        assert entry_of(view, "storage")["mutable"] is False

        async def upload() -> Any:
            async with client(cli.app) as c:
                return await c.post(
                    "/api/qcli/fs/upload", data={"path": "/a.txt"}, files={"file": ("a.txt", b"\0" * 500)},
                )

        assert (await upload()).status_code == 200
        await cli.config.update("filesystem", "filesystem", {"changes": {"maxUploadBytes": 100}}, ALL)
        res = await upload()
        assert res.status_code == 413
        assert "100 bytes" in res.text
        err = await refused(cli.config.update("filesystem", "filesystem", {"changes": {"maxUploadBytes": 1.5}}, ALL))
        assert err.status == 400
        err = await refused(cli.config.update("filesystem", "filesystem", {"changes": {"allowedPaths": ["/"]}}, ALL))
        assert err.errors == {"allowedPaths": "read-only"}
        await cli.config.update("filesystem", "filesystem", {"changes": {"maxUploadBytes": None}}, ALL)
        assert (await upload()).status_code == 200


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


class TestJobs:
    async def test_changes_the_scheduler_registers_the_prune_job_validates_durations(self) -> None:
        jobs = CliJobsBuilder().build()
        cli = create_cli_server(CliServerOptions())
        cli.mount_plugin(jobs)
        view = section(await cli.config.get("jobs", ALL), "history")
        assert value_of(view, "maxAge") is None
        assert value_of(view, "pruneInterval") == "1d"
        assert jobs.scheduler.history_retention is None
        assert PRUNE_HISTORY_JOB_ID not in jobs.scheduler.registrations

        await cli.config.update("jobs", "history", {"changes": {"maxAge": "30d", "maxRunsPerJob": 100}}, ALL)
        retention = jobs.scheduler.history_retention
        assert retention is not None
        assert (retention.max_age, retention.max_runs_per_job) == ("30d", 100)
        assert (retention.failed_max_age, retention.failed_max_runs_per_job) == (None, None)
        assert PRUNE_HISTORY_JOB_ID in jobs.scheduler.registrations

        assert (await refused(cli.config.update("jobs", "history", {"changes": {"maxAge": "500ms"}}, ALL))).status == 400
        err = await refused(cli.config.update("jobs", "history", {"changes": {"maxRunsPerJob": 0}}, ALL))
        assert err.errors == {"maxRunsPerJob": "must be at least 1"}
        err = await refused(cli.config.update("jobs", "history", {"changes": {"pruneInterval": "1h"}}, ALL))
        assert err.errors == {"pruneInterval": "read-only"}

        await cli.config.update("jobs", "history", {"changes": {"maxAge": None}}, ALL)
        retention = jobs.scheduler.history_retention
        assert (retention.max_age, retention.max_runs_per_job) == (None, 100)

    async def test_defaults_come_from_the_plugin_options(self) -> None:
        jobs = CliJobsBuilder().set_history_retention(JobHistoryRetention(
            max_age=datetime.timedelta(days=7), failed_max_runs_per_job=5, prune_interval="6h",
        )).build()
        cli = create_cli_server(CliServerOptions())
        cli.mount_plugin(jobs)
        view = section(await cli.config.get("jobs", ALL), "history")
        assert value_of(view, "maxAge") == "7d"
        assert value_of(view, "failedMaxRunsPerJob") == 5
        assert value_of(view, "pruneInterval") == "6h"


# ---------------------------------------------------------------------------
# logs (admin plugin)
# ---------------------------------------------------------------------------


def _log(message: str, level: str = "INFO") -> LogEntry:
    return LogEntry(timestamp=datetime.datetime.now(datetime.UTC).isoformat(), level=level, message=message, logger_name="t")


class TestLogs:
    async def test_capacity_and_lowest_level_change_the_buffer(self) -> None:
        buffer = LogRingBuffer(150)
        provider = create_logs_config_provider(buffer)
        for i in range(150):
            buffer.add(_log(f"m{i}"))
        await provider.update("buffer", {"capacity": 100}, None)
        assert buffer.capacity == 100
        kept, _ = buffer.query(limit=1000)
        assert len(kept) == 100
        assert kept[0]["message"] == "m50"
        assert kept[99]["message"] == "m149"
        buffer.add(_log("next"))
        assert buffer.query(limit=1000)[0][-1]["message"] == "next"

        await provider.update("buffer", {"minLevel": "WARN"}, None)
        buffer.add(_log("dropped", "INFO"))
        buffer.add(_log("kept", "ERROR"))
        messages = [e["message"] for e in buffer.query(limit=1000)[0]]
        assert "dropped" not in messages
        assert messages[-1] == "kept"

        assert provider.validate("buffer", {"capacity": 150.5}) == {"capacity": "must be a whole number"}
        await provider.update("buffer", {"capacity": None, "minLevel": None}, None)
        assert buffer.capacity == 150
        assert buffer.min_level == "DEBUG"

    def test_resize_grows_and_keeps_order_invalid_sizes_and_levels_raise(self) -> None:
        buffer = LogRingBuffer(3)
        for m in ["a", "b", "c", "d"]:
            buffer.add(_log(m))
        buffer.resize(5)
        buffer.add(_log("e"))
        assert [e["message"] for e in buffer.query(limit=10)[0]] == ["b", "c", "d", "e"]
        with pytest.raises(ValueError):
            buffer.resize(0)
        with pytest.raises(ValueError):
            buffer.min_level = "TRACE"

    async def test_through_the_service_the_server_validates_the_capacity_range(self) -> None:
        buffer = LogRingBuffer(150)
        cli = create_cli_server(CliServerOptions(configure=lambda b: b.add_config_provider(
            create_logs_config_provider(buffer),
        )))
        err = await refused(cli.config.update("logs", "buffer", {"changes": {"capacity": 10}}, ALL))
        assert err.errors == {"capacity": "must be at least 100"}
        err = await refused(cli.config.update("logs", "buffer", {"changes": {"minLevel": "TRACE"}}, ALL))
        assert err.status == 400
        await cli.config.update("logs", "buffer", {"changes": {"capacity": 200, "minLevel": "ERROR"}}, ALL)
        assert (buffer.capacity, buffer.min_level) == (200, "ERROR")


# ---------------------------------------------------------------------------
# users (Python-only here: the Node file does not cover the users provider)
# ---------------------------------------------------------------------------


class TestUsers:
    async def test_session_timeout_and_password_rules_change_at_runtime(self) -> None:
        users = CliUsersBuilder().set_default_admin_password("admin123").build()
        cli = create_cli_server(CliServerOptions())
        cli.mount_plugin(users)
        auth = users.auth_service
        sessions = section(await cli.config.get("users", ALL), "sessions")
        assert value_of(sessions, "sessionTimeout") is not None
        access = section(await cli.config.get("users", ALL), "access")
        assert access["mutable"] is False

        await cli.config.update("users", "sessions", {"changes": {"sessionTimeout": "15m"}}, ALL)
        assert auth.session_timeout_ms == 15 * 60_000

        assert auth.validate_password_policy("pass12345") is None
        await cli.config.update("users", "security", {"changes": {"minPasswordLength": 12, "maxLoginAttempts": 3}}, ALL)
        assert auth.validate_password_policy("pass12345") == "password must be at least 12 characters"
        assert auth.options.max_login_attempts == 3

        err = await refused(cli.config.update("users", "security", {"changes": {"minPasswordLength": 2.5}}, ALL))
        assert err.errors == {"minPasswordLength": "must be a whole number"}
        err = await refused(cli.config.update("users", "sessions", {"changes": {"sessionTimeout": "5s"}}, ALL))
        assert err.status == 400
        err = await refused(cli.config.update("users", "access", {"changes": {"protectServer": False}}, ALL))
        assert err.status == 400

        await cli.config.update("users", "security", {"changes": {"minPasswordLength": None}}, ALL)
        assert auth.validate_password_policy("pass12345") is None

    async def test_a_longer_minimum_refuses_short_passwords_over_http(self) -> None:
        users = CliUsersBuilder().set_default_admin_password("admin123").build()
        cli = create_cli_server(CliServerOptions())
        cli.mount_plugin(users)
        await cli.config.update("users", "security", {"changes": {"minPasswordLength": 20}}, ALL)
        async with client(cli.app) as c:
            login = await c.post("/api/v1/qcli/sessions", json={"username": "root", "password": "admin123"})
            assert login.status_code == 201, login.text
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            res = await c.post("/api/v1/qcli/users", headers=headers, json={
                "name": "ann", "email": "ann@example.com", "password": "only-sixteen-chr",
            })
            assert res.status_code == 400, res.text
            assert "at least 20 characters" in res.text


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    async def test_json_file_store_a_change_survives_a_restart_and_is_applied_at_startup(self, tmp_path) -> None:
        file = tmp_path / "config.json"
        first = create_cli_server(CliServerOptions(config=CliServerConfigOptions(store=JsonFileConfigStore(file))))
        await first.config.ready()
        await first.config.update("cors", "cors", {"changes": {"allowedOrigins": ["https://kept.example"]}}, ALL)
        await first.config.update("custom", "custom", {"changes": {"motd": "hello"}}, ALL)

        second = create_cli_server(CliServerOptions(configure=lambda b: b.set_config_store(JsonFileConfigStore(file))))
        await second.config.ready()
        assert await acao(second.app, "https://kept.example") == "https://kept.example"
        cors = section(await second.config.get("cors", ALL), "cors")
        allowed = entry_of(cors, "allowedOrigins")
        assert (allowed["value"], allowed["source"]) == (["https://kept.example"], "file")
        assert cors["version"] == "1"
        assert value_of(section(await second.config.get("custom", ALL), "custom"), "motd") == "hello"

    async def test_database_store_applied_once_ready_a_plugin_mounted_later_included(self) -> None:
        db = sqlite3.connect(":memory:")
        try:
            store = CliDatabaseConfigStore(sqlite_connection(db))
            await store.write("jobs", "history", {"maxRunsPerJob": 7})
            await store.write("mcp", "mcp", {"readOnly": True})
            cli = create_cli_server(CliServerOptions(mcp=True, config=CliServerConfigOptions(store=store)))
            jobs = CliJobsBuilder().build()
            cli.mount_plugin(jobs)
            await cli.config.ready()
            retention = jobs.scheduler.history_retention
            assert retention is not None and retention.max_runs_per_job == 7 and retention.max_age is None
            assert "config_set" not in await tool_names(cli.app)
            view = section(await cli.config.get("jobs", ALL), "history")
            runs = entry_of(view, "maxRunsPerJob")
            assert (runs["value"], runs["source"]) == (7, "database")
        finally:
            db.close()

    def test_the_store_cannot_change_once_the_server_is_created(self, tmp_path) -> None:
        cli = create_cli_server(CliServerOptions())
        with pytest.raises(RuntimeError, match="the config store must be set before the server is created"):
            cli.builder.set_config_store(JsonFileConfigStore(tmp_path / "x.json"))
