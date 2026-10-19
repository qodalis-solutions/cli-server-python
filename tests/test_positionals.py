"""Positional arguments: the ``positionals`` field, the shell-word splitter
fallback, the processors that read them and the MCP command line."""

from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server, CliServerOptions
from qodalis_cli.abstractions import (
    CliProcessCommand,
    get_positional_text,
    get_positionals,
    split_shell_words,
)
from qodalis_cli.controllers.cli_controller import ExecuteRequest
from qodalis_cli.mcp.commands import _raw_command, quote_shell_word
from qodalis_cli.processors.cli_base64_command_processor import CliBase64CommandProcessor
from qodalis_cli.processors.cli_echo_command_processor import CliEchoCommandProcessor
from qodalis_cli.processors.cli_hash_command_processor import CliHashCommandProcessor
from qodalis_cli.processors.cli_http_command_processor import _url
from qodalis_cli_weather.weather_module import _get_location


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("   ", []),
        ("a b  c", ["a", "b", "c"]),
        ('"a b"', ["a b"]),
        ("'a b' c", ["a b", "c"]),
        ('x"a b"y', ["xa by"]),
        (r"my\ file", ["my file"]),
        (r'"say \"hi\""', ['say "hi"']),
        (r'"a\\b"', ["a\\b"]),
        (r'"a\nb"', ["a\\nb"]),
        (r"'a\b'", ["a\\b"]),
        ('""', [""]),
        ('"a', ['"a']),
        ("it's", ["it's"]),
        ("trailing\\", ["trailing\\"]),
        ('"a   b"  c', ["a   b", "c"]),
    ],
)
def test_split_shell_words(text: str, expected: list[str]) -> None:
    assert split_shell_words(text) == expected


def test_get_positionals_prefers_field() -> None:
    cmd = CliProcessCommand(command="x", value='"a b" c', positionals=["a b", "c"])
    assert get_positionals(cmd) == ["a b", "c"]
    cmd = CliProcessCommand(command="x", value='"ignored"', positionals=[])
    assert get_positionals(cmd) == []


def test_get_positionals_falls_back_to_value() -> None:
    assert get_positionals(CliProcessCommand(command="x", value='"a b" c')) == ["a b", "c"]
    assert get_positionals(CliProcessCommand(command="x")) == []
    assert get_positional_text(CliProcessCommand(command="x", value='"a"  "b"')) == "a b"


def test_execute_request_accepts_positionals() -> None:
    req = ExecuteRequest.model_validate({"command": "echo", "value": '"a b"', "positionals": ["a b"]})
    assert req.positionals == ["a b"]
    assert ExecuteRequest.model_validate({"command": "echo"}).positionals is None


@pytest.mark.asyncio
@pytest.mark.parametrize("positionals", [["a b"], None])
async def test_base64_encode_unquotes(positionals) -> None:
    proc = next(p for p in CliBase64CommandProcessor().processors if p.command == "encode")
    cmd = CliProcessCommand(command="base64", value='"a b"', chain_commands=["encode"], positionals=positionals)
    assert await proc.handle_async(cmd) == base64.b64encode(b"a b").decode()


@pytest.mark.asyncio
async def test_base64_decode_unquotes() -> None:
    proc = next(p for p in CliBase64CommandProcessor().processors if p.command == "decode")
    cmd = CliProcessCommand(command="base64", value="'YSBi'")
    assert await proc.handle_async(cmd) == "a b"


@pytest.mark.asyncio
async def test_hash_unquotes() -> None:
    cmd = CliProcessCommand(command="hash", value='"a b"')
    assert await CliHashCommandProcessor().handle_async(cmd) == f"sha256: {hashlib.sha256(b'a b').hexdigest()}"


@pytest.mark.asyncio
async def test_echo_uses_positionals() -> None:
    cmd = CliProcessCommand(command="echo", value='"a   b"  c', positionals=["a   b", "c"])
    assert await CliEchoCommandProcessor().handle_async(cmd) == "a   b c"


def test_http_url_is_first_positional() -> None:
    assert _url(CliProcessCommand(command="http", value='"https://x.test/a?b=1"')) == "https://x.test/a?b=1"
    assert _url(CliProcessCommand(command="http", positionals=["https://y.test"])) == "https://y.test"
    assert _url(CliProcessCommand(command="http")) is None


def test_weather_location_unquoted() -> None:
    assert _get_location(CliProcessCommand(command="weather", value='"New York"')) == "New York"
    assert _get_location(CliProcessCommand(command="weather")) == "London"


def test_execute_endpoint_with_positionals() -> None:
    def configure(builder):
        builder.add_processor(CliBase64CommandProcessor())
        builder.add_processor(CliEchoCommandProcessor())

    result = create_cli_server(CliServerOptions(configure=configure))
    with TestClient(result.app) as client:
        resp = client.post(
            "/api/v1/qcli/execute",
            json={"command": "base64", "chainCommands": ["encode"], "value": '"a b"', "positionals": ["a b"]},
        )
        assert resp.status_code == 200
        assert resp.json()["outputs"][0]["value"] == base64.b64encode(b"a b").decode()
        # older browsers: no positionals, value split like a shell
        resp = client.post("/api/v1/qcli/execute", json={"command": "echo", "value": '"a b" c'})
        assert resp.json()["outputs"][0]["value"] == "a b c"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("a/b.c:1", "a/b.c:1"),
        ("", '""'),
        ("a b", '"a b"'),
        ("it's", "\"it's\""),
        ('say "hi"', "'say \"hi\"'"),
        ('it\'s "x"', '"it\'s \\"x\\""'),
        ("a|b", '"a|b"'),
        ("C:\\dir", '"C:\\\\dir"'),
    ],
)
def test_quote_shell_word(value: str, expected: str) -> None:
    quoted = quote_shell_word(value)
    assert quoted == expected
    assert split_shell_words(quoted) == [value]


def test_raw_command_quotes_value_and_args() -> None:
    raw = _raw_command(["base64", "encode"], "a b", {"name": "x y", "n": 3, "on": True})
    assert raw == 'base64 encode "a b" --name="x y" --n=3 --on'
