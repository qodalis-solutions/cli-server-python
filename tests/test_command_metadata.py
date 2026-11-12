"""Command metadata: aliases, examples, documentation, module, icon and allowed values."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from qodalis_cli.abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
)
from qodalis_cli.controllers.cli_controller import _chain_processors
from qodalis_cli.create_cli_server import CliServerOptions, create_cli_server
from qodalis_cli.mcp.commands import command_input_schema
from qodalis_cli.mcp.core import validate_arguments
from qodalis_cli.processors.cli_hash_command_processor import CliHashCommandProcessor
from qodalis_cli.services.cli_command_registry import CliCommandRegistry


class ShowProcessor(CliCommandProcessor):
    def __init__(self, command: str = "show") -> None:
        self._command = command

    @property
    def command(self) -> str:
        return self._command

    @property
    def description(self) -> str:
        return "Shows a thing"

    @property
    def aliases(self) -> list[str]:
        return ["sh"]

    @property
    def required_permissions(self) -> list[str]:
        return ["things:read"]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return f"show {command.value or ''}".strip()


class ThingsProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "things"

    @property
    def description(self) -> str:
        return "Manages things"

    @property
    def aliases(self) -> list[str]:
        return ["th"]

    @property
    def module(self) -> str:
        return "inventory"

    @property
    def icon(self) -> str:
        return "📦"

    @property
    def documentation(self) -> str:
        return "First paragraph.\n\nSecond paragraph."

    @property
    def examples(self) -> list[CliCommandExample]:
        return [CliCommandExample("things show 1", "Shows thing 1"), CliCommandExample("things")]

    @property
    def parameters(self):
        return [
            CliCommandParameterDescriptor("format", "Output format", aliases=["f"], default_value="table",
                                          allowed_values=["table", "json"]),
            CliCommandParameterDescriptor("verbose", "More output", type="boolean"),
        ]

    @property
    def processors(self):
        return [ShowProcessor()]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return "things"


def _client() -> TestClient:
    result = create_cli_server(CliServerOptions(configure=lambda b: b.add_processor(ThingsProcessor())))
    return TestClient(result.app)


def test_commands_send_the_metadata() -> None:
    things = next(c for c in _client().get("/api/v1/qcli/commands").json() if c["command"] == "things")
    assert things["aliases"] == ["th"]
    assert things["module"] == "inventory"
    assert things["icon"] == "📦"
    assert things["documentation"] == "First paragraph.\n\nSecond paragraph."
    assert things["examples"] == [{"command": "things show 1", "description": "Shows thing 1"}, {"command": "things"}]
    assert things["parameters"][0]["allowedValues"] == ["table", "json"]
    assert "allowedValues" not in things["parameters"][1]
    sub = things["processors"][0]
    assert sub["aliases"] == ["sh"]
    assert "examples" not in sub and "module" not in sub


def test_runs_a_command_and_a_sub_command_by_alias() -> None:
    client = _client()
    root = client.post("/api/v1/qcli/execute", json={"command": "TH", "chainCommands": [], "args": {}}).json()
    assert root["exitCode"] == 0 and root["outputs"][0]["value"] == "things"
    sub = client.post("/api/v1/qcli/execute",
                      json={"command": "th", "chainCommands": ["sh"], "value": "7", "args": {}}).json()
    assert sub["exitCode"] == 0 and sub["outputs"][0]["value"] == "show 7"


def test_a_command_name_wins_over_an_alias() -> None:
    registry = CliCommandRegistry()
    registry.register(ThingsProcessor())
    registry.register(ShowProcessor("th"))
    assert registry.find_processor("th").description == "Shows a thing"


def test_an_alias_carries_the_permissions_of_its_command() -> None:
    registry = CliCommandRegistry()
    registry.register(ThingsProcessor())
    root = registry.find_processor("th")
    assert root is not None and root.command == "things"
    chain = _chain_processors(root, ["sh"])
    assert [p.command for p in chain] == ["show"]


def test_mcp_schema_lists_allowed_values_as_enum() -> None:
    schema = command_input_schema(ThingsProcessor())
    assert schema["properties"]["format"]["enum"] == ["table", "json"]
    assert validate_arguments({"format": "xml"}, schema) == "argument 'format' must be one of: table, json"
    assert validate_arguments({"format": "json"}, schema) is None


def test_built_ins_carry_examples_and_documentation() -> None:
    hash_proc = CliHashCommandProcessor()
    assert hash_proc.examples and hash_proc.documentation
    assert hash_proc.parameters[0].allowed_values == ["md5", "sha1", "sha256", "sha512"]
