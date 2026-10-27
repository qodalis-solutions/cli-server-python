from __future__ import annotations

import asyncio
import inspect
from typing import Any

from qodalis_cli_server_abstractions import CliProcessCommand

from qodalis_cli_aws import AwsModule
from qodalis_cli_aws.processors.sqs_processor import _SqsListProcessor


def _walk(processor: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every processor of the tree with its full command path."""
    full = f"{path} {processor.command}".strip()
    found = [(full, processor)]
    for child in processor.processors or []:
        found.extend(_walk(child, full))
    return found


def _all_processors() -> list[tuple[str, Any]]:
    return [entry for root in AwsModule().processors for entry in _walk(root)]


class TestAwsProcessorTree:
    def test_module_has_the_aws_root_command(self) -> None:
        roots = AwsModule().processors
        assert [p.command for p in roots] == ["aws"]

    def test_every_service_is_registered(self) -> None:
        services = {p.command for p in AwsModule().processors[0].processors}
        assert {"configure", "status", "s3", "ec2", "lambda", "cloudwatch", "sns", "sqs", "iam", "dynamodb", "ecs"} <= services

    def test_every_processor_has_a_command_and_description(self) -> None:
        for path, processor in _all_processors():
            assert processor.command, f"empty command under '{path}'"
            assert processor.description, f"'{path}' has no description"

    def test_sibling_commands_are_unique(self) -> None:
        for path, processor in _all_processors():
            children = [c.command for c in processor.processors or []]
            assert len(children) == len(set(children)), f"duplicate sub-commands under '{path}': {children}"

    def test_parameter_names_are_unique(self) -> None:
        for path, processor in _all_processors():
            names = [p.name for p in processor.parameters or []]
            assert len(names) == len(set(names)), f"duplicate parameters on '{path}': {names}"

    def test_handlers_accept_a_cancellation_event(self) -> None:
        for path, processor in _all_processors():
            params = inspect.signature(processor.handle_async).parameters
            assert "cancellation_event" in params, f"'{path}' handle_async takes no cancellation_event"


class _FakeSqsClient:
    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    def list_queues(self) -> dict[str, Any]:
        return {"QueueUrls": self._urls}


class _FakeCredentialManager:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.requested: list[tuple[str, str | None]] = []

    def get_client(self, service: str, region: str | None = None) -> Any:
        self.requested.append((service, region))
        return self.client


class TestSqsList:
    def test_lists_the_queues_of_the_requested_region(self) -> None:
        url = "https://sqs.eu-north-1.amazonaws.com/123456789012/orders"
        manager = _FakeCredentialManager(_FakeSqsClient([url]))
        processor = _SqsListProcessor(manager)  # type: ignore[arg-type]

        result = asyncio.run(
            processor.handle_structured_async(CliProcessCommand(command="aws sqs list", args={"region": "eu-north-1"}))
        )

        assert manager.requested == [("sqs", "eu-north-1")]
        assert url in str(result)

    def test_reports_when_there_are_no_queues(self) -> None:
        processor = _SqsListProcessor(_FakeCredentialManager(_FakeSqsClient([])))  # type: ignore[arg-type]

        result = asyncio.run(processor.handle_structured_async(CliProcessCommand(command="aws sqs list")))

        assert "No SQS queues found." in str(result)
