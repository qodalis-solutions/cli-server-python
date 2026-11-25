"""GET /commands lists a command added by a module under the module's name (``module``), unless
the command names a module itself: the aws module, the demo's own commands (demo module) and the
built-in http command (core)."""

from __future__ import annotations

from typing import Sequence

from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.processors import CliHttpCommandProcessor
from qodalis_cli.services.cli_command_registry import CliCommandRegistry
from qodalis_cli_aws import AwsModule
from qodalis_cli_server_abstractions import CliCommandProcessor, CliModule, ICliCommandProcessor

from .test_demo_public_api import _load_demo


class _Cmd(CliCommandProcessor):
    def __init__(self, command: str, module: str | None = None) -> None:
        self._command = command
        self._module = module

    @property
    def command(self) -> str:
        return self._command

    @property
    def description(self) -> str:
        return self._command

    @property
    def module(self) -> str | None:
        return self._module

    async def handle_async(self, command, signal=None):  # noqa: ANN001
        return self._command


class _Shop(CliModule):
    def __init__(self) -> None:
        self._processors = [_Cmd("orders"), _Cmd("stats", "reports")]

    @property
    def name(self) -> str:
        return "shop"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Shop"

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        return self._processors


def _modules(configure) -> dict[str, str | None]:  # noqa: ANN001
    result = create_cli_server(CliServerOptions(configure=configure))
    with TestClient(result.app) as client:
        res = client.get("/api/v1/qcli/commands")
        assert res.status_code == 200
        return {c["command"]: c.get("module") for c in res.json()}


def test_module_commands_carry_its_name_own_module_wins_standalone_have_none():
    modules = _modules(lambda b: b.add_module(_Shop()).add_processor(_Cmd("loose")))

    assert modules["orders"] == "shop"
    assert modules["stats"] == "reports"
    assert modules["loose"] is None


def test_registry_records_the_module_a_processor_was_registered_with():
    registry = CliCommandRegistry()
    a, b = _Cmd("a"), _Cmd("b")
    registry.register(a, "mod")
    registry.register(b)

    assert registry.module_of(a) == "mod"
    assert registry.module_of(b) is None
    # registered again on its own, it no longer belongs to the module
    registry.register(a)
    assert registry.module_of(a) is None
    # a processor replaced under the same command name does not inherit the module
    registry.register(a, "mod")
    other = _Cmd("a")
    assert registry.module_of(other) is None


def test_aws_is_listed_under_the_aws_module():
    assert _modules(lambda b: b.add_module(AwsModule()))["aws"] == "aws"


def test_builtin_http_is_listed_under_core():
    assert _modules(lambda b: b.add_processor(CliHttpCommandProcessor()))["http"] == "core"


def test_demo_commands_are_the_demo_module():
    result = _load_demo().create_demo_server()

    demo = next(m for m in result.builder.modules if m.name == "demo")
    demo_commands = ["echo", "status", "time", "hello", "math", "system", "hash", "base64"]
    assert [p.command for p in demo.processors] == demo_commands
    for command in demo_commands:
        processor = result.registry.find_processor(command)
        assert processor.module is None
        assert result.registry.module_of(processor) == "demo"
    assert result.registry.module_of(result.registry.find_processor("aws")) == "aws"
    assert result.registry.find_processor("http").module == "core"
