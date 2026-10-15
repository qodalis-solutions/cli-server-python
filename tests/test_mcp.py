"""MCP endpoint (``POST /api/qcli/mcp``)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliMcpOptions,
    CliServerOptions,
    InMemoryFileStorageProvider,
    McpTool,
    McpToolResult,
    create_cli_server,
)
from qodalis_cli.abstractions import CliCommandProcessor
from qodalis_cli.mcp import SUPPORTED_PROTOCOL_VERSIONS, command_tool_name, object_schema
from qodalis_cli.models import CliServerResponse
from qodalis_cli.models.cli_server_output import (
    CliServerJsonOutput,
    CliServerKeyValueEntry,
    CliServerKeyValueOutput,
    CliServerListOutput,
    CliServerTableOutput,
    CliServerTextOutput,
)
from qodalis_cli_jobs import CliJobsBuilder
from qodalis_cli_server_abstractions.jobs import CliJobOptions, ICliJob
from qodalis_cli_users import CliUsersBuilder

MCP = "/api/qcli/mcp"
BASE = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
ALLOWED = "http://allowed.test"


# ---------------------------------------------------------------------------
# Test processors
# ---------------------------------------------------------------------------


class Proc(CliCommandProcessor):
    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        subs: list | None = None,
        params: list | None = None,
        value_required: bool | None = None,
        perms: list[str] | None = None,
        response: CliServerResponse | None = None,
        result: str | None = None,
        fail: bool = False,
    ) -> None:
        self._name, self._description, self._subs = name, description, subs
        self._params, self._value_required, self._perms = params, value_required, perms
        self._response, self._result, self._fail = response, result, fail
        self.calls: list[Any] = []

    @property
    def command(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def processors(self):
        return self._subs

    @property
    def parameters(self):
        return self._params

    @property
    def value_required(self):
        return self._value_required

    @property
    def required_permissions(self):
        return self._perms

    async def handle_structured_async(self, command, cancellation_event=None):
        self.calls.append(command)
        if self._fail:
            raise RuntimeError("kaboom")
        if self._response is not None:
            return self._response
        return self._STRUCTURED_NOT_IMPLEMENTED

    async def handle_async(self, command, cancellation_event=None) -> str:
        if self._result is not None:
            return self._result
        return f"{self._name}:{command.value}:{sorted(command.args.items())}"


def P(name, description, type_="string", required=False, default=None):
    return CliCommandParameterDescriptor(name=name, description=description, required=required,
                                         type=type_, default_value=default)


def _processors() -> dict[str, Proc]:
    current = Proc("current", description="Current weather", params=[
        P("city", "City name", required=True),
        P("days", "Forecast days", "number", default=1),
        P("metric", "Metric units", "boolean"),
        P("tags", "Tags", "array"),
        P("when", "Date", "date"),
    ])
    return {
        "weather": Proc("weather", description="Weather", subs=[current, Proc("alerts")]),
        "current": current,
        "echo": Proc("echo", description="Echo", value_required=False),
        "say": Proc("say", description="Say", value_required=True),
        "Odd Name!": Proc("Odd Name!", description="odd"),
        "long": Proc("x" * 80, description="long"),
        "fail": Proc("fail", description="Fails", fail=True),
        "exit1": Proc("exit1", description="exit 1", response=CliServerResponse(
            exitCode=1, outputs=[CliServerTextOutput(value="bad", style="error")])),
        "json": Proc("json", description="json", response=CliServerResponse(
            exitCode=0, outputs=[CliServerJsonOutput(value={"a": 1, "b": [1, 2]})])),
        "jsonlist": Proc("jsonlist", description="json list", response=CliServerResponse(
            exitCode=0, outputs=[CliServerJsonOutput(value=[1, 2])])),
        "mixed": Proc("mixed", description="mixed", response=CliServerResponse(exitCode=0, outputs=[
            CliServerTextOutput(value="hello"),
            CliServerTableOutput(headers=["A", "B"], rows=[["1", "2"], ["3", "4"]]),
            CliServerListOutput(items=["x", "y"]),
            CliServerKeyValueOutput(entries=[CliServerKeyValueEntry(key="k", value="v")]),
        ])),
        "deploy": Proc("deploy", description="Deploys", perms=["deploy:run"], result="deployed"),
        "admin": Proc("admin", perms=["admin:x"], subs=[Proc("reset", description="Reset", perms=["admin:reset"])]),
    }


def _server(mcp: Any = True, users: bool = False, fs: bool = False, jobs: bool = False, **opts: Any):
    procs = _processors()

    def configure(builder):
        for key, proc in procs.items():
            if key != "current":
                builder.add_processor(proc)
        if fs:
            builder.set_file_storage_provider(InMemoryFileStorageProvider())

    result = create_cli_server(CliServerOptions(configure=configure, mcp=mcp, **opts))
    jobs_plugin = None
    if jobs:
        jobs_plugin = CliJobsBuilder().add_job(NoopJob(), CliJobOptions(
            name="noop", description="Does nothing", interval="1h",
        )).build()
        result.mount_plugin(jobs_plugin)
    users_plugin = None
    if users:
        users_plugin = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build()
        result.mount_plugin(users_plugin)
    return result, procs, users_plugin, jobs_plugin


class NoopJob(ICliJob):
    async def execute_async(self, context) -> None:
        return None


class Client:
    def __init__(self, client: TestClient, token: str | None = None) -> None:
        self.c = client
        self.token = token
        self._id = 0

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(extra or {})
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def post(self, body: Any, headers: dict[str, str] | None = None):
        return self.c.post(MCP, json=body, headers=self.headers(headers))

    def rpc(self, method: str, params: Any = None, **kw: Any) -> dict[str, Any]:
        self._id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        res = self.post(body, **kw)
        assert res.status_code == 200, res.text
        assert res.headers["content-type"].startswith("application/json")
        data = res.json()
        assert data["jsonrpc"] == "2.0" and data["id"] == self._id
        return data

    def tools(self) -> dict[str, dict[str, Any]]:
        return {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}

    def call(self, name: str, arguments: Any = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.rpc("tools/call", params)


@pytest.fixture()
def open_srv():
    result, procs, _, _ = _server(fs=True)
    with TestClient(result.app) as client:
        yield Client(client), procs, result


# =========================================================================
# Transport
# =========================================================================


class TestTransport:
    def test_disabled_by_default(self):
        result = create_cli_server(CliServerOptions())
        with TestClient(result.app) as client:
            assert client.post(MCP, json={}).status_code == 404
        result = create_cli_server(CliServerOptions(mcp=CliMcpOptions(enabled=False)))
        with TestClient(result.app) as client:
            assert client.post(MCP, json={}).status_code == 404

    @pytest.mark.parametrize("requested,expected", [
        ("2025-06-18", "2025-06-18"),
        ("2025-03-26", "2025-03-26"),
        ("1999-01-01", "2025-11-25"),
    ])
    def test_initialize_version_negotiation(self, open_srv, requested, expected):
        c, _, _ = open_srv
        params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        if requested:
            params["protocolVersion"] = requested
        res = c.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        assert res.status_code == 200
        assert "mcp-session-id" not in {k.lower() for k in res.headers}
        result = res.json()["result"]
        assert result["protocolVersion"] == expected
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"]["name"] == "qodalis-cli-server"
        assert isinstance(result["serverInfo"]["version"], str)
        assert "instructions" not in result

    def test_supported_versions(self):
        assert SUPPORTED_PROTOCOL_VERSIONS == ["2025-11-25", "2025-06-18", "2025-03-26"]

    @pytest.mark.parametrize("params", [{}, {"protocolVersion": 5}])
    def test_initialize_requires_protocol_version(self, open_srv, params):
        c, _, _ = open_srv
        assert c.rpc("initialize", params)["error"]["code"] == -32602

    def test_server_version_is_package_version(self, open_srv):
        from importlib.metadata import version

        c, _, _ = open_srv
        info = c.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]
        assert info["version"] == version("qodalis-cli-server")

    def test_instructions(self):
        result, *_ = _server(mcp=CliMcpOptions(instructions="Use the tools wisely"))
        with TestClient(result.app) as client:
            init = Client(client).rpc("initialize", {"protocolVersion": "2025-06-18"})
            assert init["result"]["instructions"] == "Use the tools wisely"

    def test_dict_options(self):
        result, *_ = _server(mcp={"tools": ["echo"]})
        with TestClient(result.app) as client:
            assert set(Client(client).tools()) == {"echo"}

    def test_ping(self, open_srv):
        c, _, _ = open_srv
        assert c.rpc("ping")["result"] == {}

    def test_notifications_202(self, open_srv):
        c, _, _ = open_srv
        for method in ("notifications/initialized", "notifications/cancelled", "unknown/notification"):
            res = c.post({"jsonrpc": "2.0", "method": method})
            assert res.status_code == 202
            assert res.content == b""

    def test_client_response_202(self, open_srv):
        c, _, _ = open_srv
        assert c.post({"jsonrpc": "2.0", "id": 5, "result": {}}).status_code == 202

    def test_parse_error(self, open_srv):
        c, _, _ = open_srv
        res = c.c.post(MCP, content=b"{not json", headers={"content-type": "application/json"})
        assert res.status_code == 200
        assert res.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

    @pytest.mark.parametrize("body", [
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": {"x": 1}, "method": "ping"},
        "ping",
    ])
    def test_invalid_request(self, open_srv, body):
        c, _, _ = open_srv
        res = c.post(body)
        assert res.status_code == 200
        assert res.json()["error"]["code"] == -32600

    def test_unknown_method(self, open_srv):
        c, _, _ = open_srv
        data = c.rpc("resources/list")
        assert data["error"]["code"] == -32601

    def test_bad_params(self, open_srv):
        c, _, _ = open_srv
        assert c.rpc("tools/list", [1, 2])["error"]["code"] == -32602
        assert c.rpc("tools/call", {})["error"]["code"] == -32602
        assert c.rpc("tools/call", {"name": "echo", "arguments": [1]})["error"]["code"] == -32602

    def test_string_ids(self, open_srv):
        c, _, _ = open_srv
        res = c.post({"jsonrpc": "2.0", "id": "abc", "method": "ping"})
        assert res.json() == {"jsonrpc": "2.0", "id": "abc", "result": {}}

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    def test_get_delete_405(self, open_srv, method):
        c, _, _ = open_srv
        res = c.c.request(method, MCP)
        assert res.status_code == 405
        assert res.headers["allow"] == "POST"

    def test_origin(self):
        result, *_ = _server(allowed_origins=[ALLOWED])
        with TestClient(result.app) as client:
            c = Client(client)
            res = c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "http://evil.test"})
            assert res.status_code == 403
            assert res.json() == {"error": "forbidden: origin not allowed"}
            assert c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          headers={"Origin": ALLOWED}).status_code == 200
            # Same-origin is always allowed; no Origin (non-browser) too.
            assert c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          headers={"Origin": "http://testserver"}).status_code == 200
            assert c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200

    def test_origin_without_allowed_origins(self, open_srv):
        c, _, _ = open_srv
        res = c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "http://evil.test"})
        assert res.status_code == 403


class TestStaticToken:
    def test_static_token_required(self):
        result, *_ = _server(mcp=CliMcpOptions(token="s3cret-token"))
        with TestClient(result.app) as client:
            anon = Client(client)
            res = anon.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert res.status_code == 401
            assert res.json() == {"error": "unauthorized"}
            assert Client(client, "wrong").post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
            ok = Client(client, "s3cret-token")
            assert ok.rpc("ping")["result"] == {}
            assert "echo" in ok.tools()

    def test_open_without_provider_and_token(self, open_srv):
        c, _, _ = open_srv
        assert "echo" in c.tools()
        assert c.call("echo", {"value": "hi"})["result"]["isError"] is False


# =========================================================================
# tools/list
# =========================================================================


class TestToolsList:
    def test_names_and_selection(self, open_srv):
        c, _, _ = open_srv
        tools = c.tools()
        # Parents without parameters/value are not tools; their leaves are.
        assert "weather" not in tools
        assert {"weather_current", "weather_alerts", "echo", "say", "fail", "json"} <= set(tools)
        assert "odd-name-" in tools
        assert "x" * 64 in tools
        assert "admin_reset" in tools and "admin" not in tools
        for name in tools:
            assert len(name) <= 64 and all(ch in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in name)

    def test_tool_name_rules(self):
        assert command_tool_name(["Weather", "Current"]) == "weather_current"
        assert command_tool_name(["a.b", "c d"]) == "a-b_c-d"
        assert command_tool_name(["y" * 100]) == "y" * 64

    def test_collision_first_wins(self):
        result = create_cli_server(CliServerOptions(mcp=True, configure=lambda b: (
            b.add_processor(Proc("a-b", description="first")),
            b.add_processor(Proc("a.b", description="second")),
        )))
        with TestClient(result.app) as client:
            tools = Client(client).tools()
            assert tools["a-b"]["description"] == "first"

    def test_description_fallback(self, open_srv):
        c, _, _ = open_srv
        tools = c.tools()
        assert tools["weather_alerts"]["description"] == "Run 'weather alerts'"
        assert tools["weather_current"]["description"] == "Current weather"

    def test_input_schema(self, open_srv):
        c, _, _ = open_srv
        tools = c.tools()
        schema = tools["weather_current"]["inputSchema"]
        assert schema == {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "days": {"type": "number", "description": "Forecast days", "default": 1},
                "metric": {"type": "boolean", "description": "Metric units"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                "when": {"type": "string", "description": "Date"},
            },
            "required": ["city"],
            "additionalProperties": False,
        }
        assert tools["say"]["inputSchema"]["required"] == ["value"]
        assert tools["say"]["inputSchema"]["properties"]["value"]["type"] == "string"
        assert tools["echo"]["inputSchema"]["required"] == []
        assert "value" in tools["echo"]["inputSchema"]["properties"]
        assert tools["fail"]["inputSchema"] == {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
        }

    def test_allow_and_deny_lists(self):
        result, *_ = _server(mcp=CliMcpOptions(tools=["weather_*", "echo", "say"], exclude=["weather_alerts", "say"]))
        with TestClient(result.app) as client:
            assert set(Client(client).tools()) == {"weather_current", "echo"}

    def test_lists_apply_to_feature_tools(self):
        result, *_ = _server(mcp=CliMcpOptions(exclude=["fs_*", "server_info"]), fs=True)
        with TestClient(result.app) as client:
            tools = Client(client).tools()
            assert not any(n.startswith("fs_") for n in tools) and "server_info" not in tools
            assert "echo" in tools

    def test_registry_changes_are_live(self, open_srv):
        c, _, result = open_srv
        assert "late" not in c.tools()
        result.registry.register(Proc("late", description="late"))
        assert "late" in c.tools()

    def test_cursor_ignored(self, open_srv):
        c, _, _ = open_srv
        assert c.rpc("tools/list", {"cursor": "x"})["result"]["tools"]


# =========================================================================
# tools/call
# =========================================================================


class TestToolsCall:
    def test_success_builds_cli_command(self, open_srv):
        c, procs, _ = open_srv
        res = c.call("weather_current", {"city": "Paris", "days": 3, "metric": True, "tags": ["a", "b"]})
        result = res["result"]
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        cmd = procs["current"].calls[-1]
        assert cmd.command == "weather"
        assert cmd.chain_commands == ["current"]
        assert cmd.args == {"city": "Paris", "days": 3, "metric": True, "tags": ["a", "b"]}
        assert cmd.value is None
        assert cmd.raw_command.startswith("weather current --city=Paris")
        assert "structuredContent" not in result

    def test_value(self, open_srv):
        c, procs, _ = open_srv
        assert c.call("say", {"value": "hello"})["result"]["content"][0]["text"] == "say:hello:[]"
        assert procs["say"].calls[-1].value == "hello"

    def test_is_error_on_exit_code(self, open_srv):
        c, _, _ = open_srv
        result = c.call("exit1")["result"]
        assert result == {"content": [{"type": "text", "text": "bad"}], "isError": True}

    def test_exception_is_tool_error(self, open_srv):
        c, _, _ = open_srv
        result = c.call("fail")["result"]
        assert result["isError"] is True
        assert "kaboom" in result["content"][0]["text"]

    def test_unknown_tool(self, open_srv):
        c, _, _ = open_srv
        data = c.call("nope")
        assert data["error"] == {"code": -32602, "message": "Unknown tool: nope"}

    @pytest.mark.parametrize("args,reason", [
        ({"city": 5}, "argument 'city' must be a string"),
        ({"city": "x", "days": "3"}, "argument 'days' must be a number"),
        ({"city": "x", "metric": "yes"}, "argument 'metric' must be a boolean"),
        ({"city": "x", "tags": "a"}, "argument 'tags' must be an array of strings"),
        ({"city": "x", "tags": [1]}, "argument 'tags' must be an array of strings"),
        ({"city": "x", "bogus": 1}, "unknown argument 'bogus'"),
        ({}, "missing required argument 'city'"),
        ({"city": None}, "missing required argument 'city'"),
    ])
    def test_invalid_arguments(self, open_srv, args, reason):
        c, _, _ = open_srv
        error = c.call("weather_current", args)["error"]
        assert error == {"code": -32602, "message": f"Invalid arguments for tool weather_current: {reason}"}

    def test_missing_required_value(self, open_srv):
        c, _, _ = open_srv
        assert "missing required argument 'value'" in c.call("say", {})["error"]["message"]

    def test_structured_content(self, open_srv):
        c, _, _ = open_srv
        result = c.call("json")["result"]
        assert result["structuredContent"] == {"a": 1, "b": [1, 2]}
        assert '"a": 1' in result["content"][0]["text"]
        assert "structuredContent" not in c.call("jsonlist")["result"]

    def test_rendering(self, open_srv):
        c, _, _ = open_srv
        text = c.call("mixed")["result"]["content"][0]["text"]
        assert text == "hello\nA | B\n1 | 2\n3 | 4\nx\ny\nk: v"

    def test_blocked_command_is_error(self, open_srv):
        c, _, result = open_srv

        class Block:
            def is_allowed(self, processor):
                return processor.command != "echo"

        result.executor.add_filter(Block())
        res = c.call("echo", {"value": "x"})["result"]
        assert res["isError"] is True and "disabled" in res["content"][0]["text"]


# =========================================================================
# Built-in feature tools
# =========================================================================


class TestServerInfo:
    def test_server_info(self, open_srv):
        c, _, _ = open_srv
        tools = c.tools()
        assert tools["server_info"]["annotations"] == {"readOnlyHint": True}
        result = c.call("server_info")["result"]
        info = result["structuredContent"]
        assert info["name"] == "qodalis-cli-server"
        assert "filesystem" in info["features"] and "commands" in info["features"] and "mcp" in info["features"]
        assert "echo" in info["commands"]
        assert set(info["capabilities"]) >= {"os", "shell", "streaming"}


class TestFilesystemTools:
    def test_only_when_enabled(self):
        result, *_ = _server()
        with TestClient(result.app) as client:
            assert not any(n.startswith("fs_") for n in Client(client).tools())

    def test_tools_and_annotations(self, open_srv):
        c, _, _ = open_srv
        tools = c.tools()
        assert {"fs_list", "fs_stat", "fs_read", "fs_write", "fs_mkdir", "fs_delete"} <= set(tools)
        assert tools["fs_read"]["annotations"] == {"readOnlyHint": True}
        assert tools["fs_delete"]["annotations"] == {"destructiveHint": True}
        assert "annotations" not in tools["fs_write"]
        assert tools["fs_read"]["inputSchema"]["properties"]["maxBytes"]["default"] == 262144

    def test_round_trip(self, open_srv):
        c, _, _ = open_srv
        assert c.call("fs_mkdir", {"path": "/docs/sub"})["result"]["isError"] is False
        assert c.call("fs_write", {"path": "/docs/a.txt", "content": "héllo"})["result"]["isError"] is False
        assert c.call("fs_read", {"path": "/docs/a.txt"})["result"]["content"][0]["text"] == "héllo"
        listing = c.call("fs_list", {"path": "/docs"})["result"]
        assert {e["name"] for e in listing["structuredContent"]["entries"]} == {"sub", "a.txt"}
        stat = c.call("fs_stat", {"path": "/docs/a.txt"})["result"]["structuredContent"]
        assert stat["type"] == "file"
        assert c.call("fs_delete", {"path": "/docs/a.txt"})["result"]["isError"] is False
        missing = c.call("fs_read", {"path": "/docs/a.txt"})["result"]
        assert missing["isError"] is True

    def test_read_truncated_and_binary(self, open_srv):
        c, _, result = open_srv
        provider = result.builder.file_storage_provider
        asyncio.run(provider.write_file("/big.txt", "abcdef" * 10))
        text = c.call("fs_read", {"path": "/big.txt", "maxBytes": 6})["result"]["content"][0]["text"]
        assert text == "abcdef\n[truncated: 6 of 60 bytes]"
        full = c.call("fs_read", {"path": "/big.txt"})["result"]["content"][0]["text"]
        assert full == "abcdef" * 10
        asyncio.run(provider.write_file("/bin.dat", b"\x00\xff\xfe\x01"))
        res = c.call("fs_read", {"path": "/bin.dat"})["result"]
        assert res == {"content": [{"type": "text", "text": "binary file, 4 bytes"}], "isError": True}
        bad = c.call("fs_read", {"path": "/big.txt", "maxBytes": 0})["result"]
        assert bad == {"content": [{"type": "text", "text": "maxBytes must be at least 1"}], "isError": True}

    def test_path_validator_rejection(self, tmp_path):
        from qodalis_cli import FileSystemOptions

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (allowed / "a.txt").write_text("inside")
        (tmp_path / "secret.txt").write_text("outside")
        result = create_cli_server(CliServerOptions(mcp=True, configure=lambda b: b.add_filesystem(
            FileSystemOptions(allowed_paths=[str(allowed)]))))
        with TestClient(result.app) as client:
            c = Client(client)
            ok = c.call("fs_read", {"path": str(allowed / "a.txt")})["result"]
            assert ok == {"content": [{"type": "text", "text": "inside"}], "isError": False}
            for tool, args in [
                ("fs_read", {"path": str(tmp_path / "secret.txt")}),
                ("fs_read", {"path": str(allowed / ".." / "secret.txt")}),
                ("fs_list", {"path": str(tmp_path)}),
                ("fs_write", {"path": str(tmp_path / "evil.txt"), "content": "x"}),
                ("fs_delete", {"path": str(tmp_path / "secret.txt")}),
            ]:
                res = c.call(tool, args)["result"]
                assert res["isError"] is True, (tool, res)
            assert (tmp_path / "secret.txt").read_text() == "outside"
            assert not (tmp_path / "evil.txt").exists()

    def test_errors_are_tool_errors(self, open_srv):
        c, _, _ = open_srv
        res = c.call("fs_list", {"path": "/does/not/exist"})["result"]
        assert res["isError"] is True and res["content"][0]["text"]
        res = c.call("fs_delete", {"path": "/nope"})["result"]
        assert res["isError"] is True


class TestJobsTools:
    def test_jobs_tools(self):
        result, *_ , jobs = _server(jobs=True)
        with TestClient(result.app) as client:
            c = Client(client)
            tools = c.tools()
            assert {"jobs_list", "jobs_get", "jobs_history", "jobs_trigger", "jobs_pause",
                    "jobs_resume", "jobs_stop", "jobs_cancel"} <= set(tools)
            assert tools["jobs_stop"]["annotations"] == {"destructiveHint": True}
            assert tools["jobs_list"]["annotations"] == {"readOnlyHint": True}
            listing = c.call("jobs_list")["result"]["structuredContent"]["jobs"]
            assert listing[0]["name"] == "noop"
            job_id = listing[0]["id"]
            assert c.call("jobs_get", {"id": "noop"})["result"]["structuredContent"]["id"] == job_id
            assert c.call("jobs_pause", {"id": job_id})["result"] == {
                "content": [{"type": "text", "text": "Job paused"}], "isError": False}
            again = c.call("jobs_pause", {"id": job_id})["result"]
            assert again["isError"] is True and "already paused" in again["content"][0]["text"]
            assert c.call("jobs_resume", {"id": job_id})["result"]["isError"] is False
            assert c.call("jobs_get", {"id": "missing"})["result"]["isError"] is True
            history = c.call("jobs_history", {"id": job_id, "limit": 5})["result"]
            assert history["structuredContent"]["items"] == []
            assert c.call("jobs_cancel", {"id": job_id})["result"]["isError"] is True

    def test_no_jobs_tools_without_plugin(self, open_srv):
        c, _, _ = open_srv
        assert not any(n.startswith("jobs_") for n in c.tools())


class TestDataExplorerTools:
    def test_data_tools(self):
        pytest.importorskip("qodalis_cli_data_explorer")
        sql = pytest.importorskip("qodalis_cli_data_explorer_sql")
        from qodalis_cli_server_abstractions import DataExplorerProviderOptions

        def configure(b):
            b.add_data_explorer_provider(
                sql.SqlDataExplorerProvider(":memory:"),
                DataExplorerProviderOptions(name="mem", description="memory db"),
            )

        result = create_cli_server(CliServerOptions(configure=configure, mcp=True))
        with TestClient(result.app) as client:
            c = Client(client)
            tools = c.tools()
            assert {"data_sources", "data_schema", "data_query"} <= set(tools)
            sources = c.call("data_sources")["result"]["structuredContent"]["sources"]
            assert sources[0]["name"] == "mem"
            res = c.call("data_query", {"source": "mem", "query": "SELECT 1 AS one, 'x' AS two"})["result"]
            assert res["isError"] is False
            assert res["content"][0]["text"].splitlines()[0] == "one | two"
            bad = c.call("data_query", {"source": "nope", "query": "SELECT 1"})["result"]
            assert bad["isError"] is True and "Unknown data source" in bad["content"][0]["text"]
            assert c.call("data_schema", {"source": "nope"})["result"]["isError"] is True


# =========================================================================
# Provider extension API
# =========================================================================


class EchoToolProvider:
    def __init__(self, name: str = "custom_tool", text: str = "custom", perms: list[str] | None = None) -> None:
        self.tool_name, self.text, self.perms = name, text, perms or []

    async def get_tools(self, context):
        async def call(args, ctx):
            return McpToolResult(text=f"{self.text}:{args.get('n')}", structured={"n": args.get("n")})

        return [McpTool(
            name=self.tool_name,
            description="custom",
            input_schema=object_schema({"n": {"type": "integer"}}, ["n"]),
            call=call,
            required_permissions=self.perms,
        )]


class TestProviders:
    def test_add_provider(self, open_srv):
        c, _, result = open_srv
        result.mcp.add_provider(EchoToolProvider())
        assert c.call("custom_tool", {"n": 2})["result"] == {
            "content": [{"type": "text", "text": "custom:2"}], "isError": False, "structuredContent": {"n": 2},
        }
        assert c.call("custom_tool", {"n": "x"})["error"]["message"] == (
            "Invalid arguments for tool custom_tool: argument 'n' must be an integer")

    def test_collisions(self):
        result = create_cli_server(CliServerOptions(mcp=True, configure=lambda b: (
            b.add_processor(Proc("server_info", description="command")),
            b.add_processor(Proc("dup", description="command dup")),
        )))
        result.mcp.add_provider(EchoToolProvider("dup", "first-plugin"))
        result.mcp.add_provider(EchoToolProvider("dup", "second-plugin"))
        with TestClient(result.app) as client:
            c = Client(client)
            tools = c.tools()
            # Built-in feature tools win over commands.
            assert tools["server_info"]["description"] != "command"
            # Plugin tools beat command tools.
            assert tools["dup"]["description"] == "custom"
            assert c.call("dup", {"n": 1})["result"]["content"][0]["text"] == "first-plugin:1"
            # Between plugin providers the first registered wins.
            result.mcp.add_provider(EchoToolProvider("dup2", "first-plugin"))
            result.mcp.add_provider(EchoToolProvider("dup2", "second-plugin"))
            assert c.call("dup2", {"n": 1})["result"]["content"][0]["text"] == "first-plugin:1"

    def test_invalid_tool_name_ignored(self, open_srv):
        c, _, result = open_srv
        result.mcp.add_provider(EchoToolProvider("Bad Name"))
        assert "Bad Name" not in c.tools()

    def test_failing_provider_ignored(self, open_srv):
        c, _, result = open_srv

        class Broken:
            def get_tools(self, context):
                raise RuntimeError("broken")

        result.mcp.add_provider(Broken())
        assert "echo" in c.tools()


# =========================================================================
# With the users plugin (auth provider)
# =========================================================================


class Protected:
    def __init__(self, client: TestClient) -> None:
        self.c = client

    def login(self, name: str, password: str) -> str:
        res = self.c.post(BASE + "/sessions", json={"username": name, "password": password})
        assert res.status_code == 201, res.text
        return res.json()["token"]

    @property
    def root(self) -> str:
        return self.login("root", ROOT_PASS)

    def user(self, name: str, perms: list[str], *, in_users: bool = True) -> str:
        admin = self.root
        h = {"Authorization": f"Bearer {admin}"}
        assert self.c.post(BASE + "/groups", headers=h, json={"name": f"{name}-g", "permissions": perms}).status_code == 201
        groups = [f"{name}-g"] + (["users"] if in_users else [])
        res = self.c.post(BASE + "/users", headers=h, json={
            "name": name, "email": f"{name}@example.com", "password": "pass1234", "groups": groups,
        })
        assert res.status_code == 201, res.text
        return self.login(name, "pass1234")


@pytest.fixture()
def prot():
    result, procs, users, _ = _server(users=True, fs=True, jobs=True, mcp=CliMcpOptions(token="ignored-static"))
    with TestClient(result.app) as client:
        yield Protected(client), result


class TestWithUsersPlugin:
    def test_anonymous_401(self, prot):
        p, _ = prot
        res = p.c.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert res.status_code == 401
        assert set(res.json()) == {"error"}
        # The static token is ignored when an auth provider protects the server.
        res = Client(p.c, "ignored-static").post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert res.status_code == 401

    def test_commands_execute_required(self, prot):
        p, _ = prot
        token = p.user("noexec", ["files:read"], in_users=False)
        res = Client(p.c, token).post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert res.status_code == 403
        assert "commands:execute" in res.json()["error"]

    def test_tools_filtered_by_permissions(self, prot):
        p, _ = prot
        alice = Client(p.c, p.user("alice", ["files:read"]))
        tools = alice.tools()
        assert "echo" in tools and "server_info" in tools
        assert "deploy" not in tools and "admin_reset" not in tools
        assert {"fs_list", "fs_read", "fs_stat"} <= set(tools)
        assert not {"fs_write", "fs_mkdir", "fs_delete"} & set(tools)
        assert not any(n.startswith("jobs_") for n in tools)
        data = alice.call("deploy")
        assert data["error"] == {"code": -32602, "message": "Unknown tool: deploy"}
        assert alice.call("fs_write", {"path": "/x", "content": "y"})["error"]["code"] == -32602

    def test_chain_permissions(self, prot):
        p, _ = prot
        only_child = Client(p.c, p.user("child", ["admin:reset"]))
        assert "admin_reset" not in only_child.tools()
        both = Client(p.c, p.user("both", ["admin:x", "admin:reset", "deploy:run", "jobs:read"]))
        tools = both.tools()
        assert "admin_reset" in tools and "deploy" in tools
        assert "jobs_list" in tools and "jobs_trigger" not in tools
        assert both.call("deploy")["result"]["content"][0]["text"] == "deployed"

    def test_root_sees_everything(self, prot):
        p, _ = prot
        tools = Client(p.c, p.root).tools()
        assert {"deploy", "admin_reset", "fs_delete", "jobs_stop", "server_info"} <= set(tools)

    def test_api_token(self, prot):
        p, _ = prot
        root = p.root
        res = p.c.post(BASE + "/tokens", headers={"Authorization": f"Bearer {root}"},
                       json={"name": "mcp", "permissions": ["commands:execute"]})
        assert res.status_code == 201, res.text
        c = Client(p.c, res.json()["token"])
        tools = c.tools()
        assert "echo" in tools and "deploy" not in tools and "fs_list" not in tools
        assert c.call("echo", {"value": "via token"})["result"]["isError"] is False
        res = p.c.post(BASE + "/tokens", headers={"Authorization": f"Bearer {root}"}, json={"name": "full"})
        full = Client(p.c, res.json()["token"])
        assert "deploy" in full.tools()

    def test_origin_and_405_before_auth(self, prot):
        p, _ = prot
        res = p.c.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "http://evil.test"})
        assert res.status_code == 403 and res.json() == {"error": "forbidden: origin not allowed"}
        for method in ("GET", "DELETE"):
            res = p.c.request(method, MCP)
            assert res.status_code == 405 and res.headers["allow"] == "POST"

    def test_unprotected_users_plugin_uses_static_token(self):
        result = create_cli_server(CliServerOptions(mcp=CliMcpOptions(token="static"), configure=lambda b: (
            b.add_processor(Proc("deploy", description="d", perms=["deploy:run"])),
        )))
        result.mount_plugin(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_protect_server(False).build())
        with TestClient(result.app) as client:
            assert Client(client).post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
            # Not protected: no permission filtering.
            assert "deploy" in Client(client, "static").tools()

    def test_methods_still_405_when_authenticated(self, prot):
        p, _ = prot
        res = p.c.get(MCP, headers={"Authorization": f"Bearer {p.root}"})
        assert res.status_code == 405 and res.headers["allow"] == "POST"


class TestBuiltInProcessors:
    def test_builtin_value_and_parameters(self):
        from qodalis_cli import CliBase64CommandProcessor, CliEchoCommandProcessor, CliHashCommandProcessor

        result = create_cli_server(CliServerOptions(mcp=True, configure=lambda b: (
            b.add_processor(CliEchoCommandProcessor()),
            b.add_processor(CliHashCommandProcessor()),
            b.add_processor(CliBase64CommandProcessor()),
        )))
        with TestClient(result.app) as client:
            c = Client(client)
            tools = c.tools()
            assert tools["echo"]["inputSchema"]["required"] == []
            assert set(tools["hash"]["inputSchema"]["properties"]) == {"algorithm", "value"}
            assert {"base64_encode", "base64_decode"} <= set(tools)
            assert c.call("echo", {"value": "hi there"})["result"]["content"][0]["text"] == "hi there"
            text = c.call("hash", {"value": "abc", "algorithm": "md5"})["result"]["content"][0]["text"]
            assert text == "md5: 900150983cd24fb0d6963f7d28e17f72"
            assert c.call("base64_encode", {"value": "hi"})["result"]["content"][0]["text"] == "aGk="


class TestDataExplorerPermissions:
    def test_data_tools_need_data_query(self):
        pytest.importorskip("qodalis_cli_data_explorer")
        sql = pytest.importorskip("qodalis_cli_data_explorer_sql")
        from qodalis_cli_server_abstractions import DataExplorerProviderOptions

        def configure(b):
            b.add_data_explorer_provider(sql.SqlDataExplorerProvider(":memory:"),
                                         DataExplorerProviderOptions(name="mem", description="memory db"))

        result = create_cli_server(CliServerOptions(configure=configure, mcp=True))
        result.mount_plugin(CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).build())
        with TestClient(result.app) as client:
            p = Protected(client)
            plain = Client(client, p.user("plain", ["logs:read"]))
            assert not any(n.startswith("data_") for n in plain.tools())
            assert plain.call("data_query", {"source": "mem", "query": "SELECT 1"})["error"]["code"] == -32602
            analyst = Client(client, p.user("analyst", ["data:query"]))
            tools = analyst.tools()
            assert tools["data_sources"]["annotations"] == {"readOnlyHint": True}
            assert "annotations" not in tools["data_query"]
            res = analyst.call("data_query", {"source": "mem", "query": "SELECT 2 AS n"})["result"]
            assert res["isError"] is False and res["structuredContent"]["rows"]
