"""Every processor must accept the arguments the server passes to it.

The executor calls ``handle_structured_async(command, cancellation_event)``
and ``handle_async(command, cancellation_event)``; the streaming endpoint
calls ``handle_stream_async(command, emit, cancellation_event)``.  An
override that drops ``cancellation_event`` raises ``TypeError`` only at
runtime, so these tests check the signatures of every processor shipped
here: built-ins, plugin modules and demo processors, sub-processors included.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from qodalis_cli import processors as builtin_processors
from qodalis_cli.abstractions import CliProcessCommand, ICliCommandProcessor
from qodalis_cli.services import CliCommandExecutorService, CliCommandRegistry
from qodalis_cli_server_abstractions import is_stream_capable

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _root_processors() -> list[ICliCommandProcessor]:
    roots: list[ICliCommandProcessor] = [
        getattr(builtin_processors, name)() for name in builtin_processors.__all__
    ]

    from plugins.weather import WeatherModule

    roots.extend(WeatherModule().processors)

    try:
        import boto3  # noqa: F401
    except ImportError:
        pass
    else:
        from plugins.aws.qodalis_cli_aws import AwsModule

        roots.extend(AwsModule().processors)

    sys.path.insert(0, os.path.join(_REPO_ROOT, "demo"))
    try:
        import processors as demo_processors
    finally:
        sys.path.pop(0)
    roots.extend(getattr(demo_processors, name)() for name in demo_processors.__all__)
    return roots


def _walk(processors: list[ICliCommandProcessor], path: str = "") -> Iterator[tuple[str, ICliCommandProcessor]]:
    for processor in processors:
        full = f"{path} {processor.command}".strip()
        yield full, processor
        yield from _walk(list(processor.processors or []), full)


_ALL = list(_walk(_root_processors()))


def _can_call(method: Any, *args: Any) -> bool:
    try:
        inspect.signature(method).bind(*args)
    except TypeError:
        return False
    return True


def test_collects_plugin_sub_processors() -> None:
    names = {name for name, _ in _ALL}
    assert {"base64 encode", "http get", "weather current", "math add"} <= names
    if "aws" in names:
        assert "aws s3 ls" in names


@pytest.mark.parametrize("name,processor", _ALL, ids=[name for name, _ in _ALL])
def test_processor_accepts_executor_arguments(name: str, processor: ICliCommandProcessor) -> None:
    command = CliProcessCommand(command=name)
    event = asyncio.Event()

    assert _can_call(processor.handle_async, command, event), (
        f"{type(processor).__name__}.handle_async must accept (command, cancellation_event)"
    )
    assert _can_call(processor.handle_structured_async, command, event), (
        f"{type(processor).__name__}.handle_structured_async must accept (command, cancellation_event)"
    )
    if is_stream_capable(processor):
        assert _can_call(processor.handle_stream_async, command, lambda _: None, event), (
            f"{type(processor).__name__}.handle_stream_async must accept (command, emit, cancellation_event)"
        )


class TestAwsStructuredExecution:
    """Runs an AWS command through the executor with a mocked boto3 client."""

    @pytest.fixture
    def aws(self) -> Any:
        pytest.importorskip("boto3")
        from plugins.aws.qodalis_cli_aws import AwsModule

        (processor,) = AwsModule().processors
        return processor

    @pytest.mark.asyncio
    async def test_sqs_list_via_executor(self, aws: Any) -> None:
        client = MagicMock()
        client.list_queues.return_value = {
            "QueueUrls": ["https://sqs.eu-north-1.amazonaws.com/123/orders"],
        }
        aws.get_credential_manager().get_client = MagicMock(return_value=client)

        registry = CliCommandRegistry()
        registry.register(aws)
        executor = CliCommandExecutorService(registry)

        response = await executor.execute_async(
            CliProcessCommand(command="aws", chain_commands=["sqs", "list"]),
            cancellation_event=asyncio.Event(),
        )

        assert response.exit_code == 0, response.outputs
        client.list_queues.assert_called_once()
        rendered = str([o.model_dump() for o in response.outputs])
        assert "orders" in rendered
