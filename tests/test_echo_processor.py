"""The echo processor prints a quoted value without its quotes, as the browser's echo does."""

import pytest

from qodalis_cli.abstractions import CliProcessCommand
from qodalis_cli.processors.cli_echo_command_processor import (
    CliEchoCommandProcessor,
    join_shell_words,
)


async def _echo(value: str | None) -> str:
    command = CliProcessCommand(command="echo", raw_command=f"echo {value or ''}", value=value)
    return await CliEchoCommandProcessor().handle_async(command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('"hello world"', "hello world"),
        ("'a b'", "a b"),
        ('"a --b"', "a --b"),
        ("hello world", "hello world"),
        ('"a', '"a'),
        ("\"a'", "\"a'"),
        ("-", "-"),
        ('"a" "b"', "a b"),
        ('"a   b"  c', "a   b c"),
        ("it's fine", "it's fine"),
    ],
)
async def test_echo_value(value: str, expected: str) -> None:
    assert await _echo(value) == expected


@pytest.mark.asyncio
async def test_echo_without_value_prints_usage() -> None:
    assert await _echo(None) == "Usage: echo <text>"


def test_join_shell_words_lone_quote() -> None:
    assert join_shell_words('"') == '"'
    assert join_shell_words('""') == ""
