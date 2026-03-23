from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)

from ..services.aws_credential_manager import AwsCredentialManager
from ..utils.output_helpers import (
    build_response,
    build_error_response,
    build_success_response,
    format_as_json,
    format_as_list,
    apply_output_format,
    is_dry_run,
    CliServerTextOutput,
)


class _SqsListProcessor(CliCommandProcessor):
    """Lists all SQS queue URLs."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return "List SQS queues"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (list|json|text)", required=False, type="string", aliases=["-o"], default_value="list"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns all SQS queue URLs."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("sqs", region=str(region) if region else None)

        try:
            response = client.list_queues()
            queue_urls = response.get("QueueUrls", [])

            if not queue_urls:
                return build_response([CliServerTextOutput(value="No SQS queues found.", style="warning")])

            list_output = format_as_list(queue_urls)
            return build_response([apply_output_format(command, list_output, queue_urls)])
        except Exception as exc:
            return build_error_response(f"Failed to list SQS queues: {exc}")


class _SqsSendProcessor(CliCommandProcessor):
    """Sends a message to an SQS queue."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "send"

    @property
    def description(self) -> str:
        return "Send a message to an SQS queue"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="message", description="Message body to send", required=True, type="string", aliases=["-m"]),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Sends the provided message body to the specified SQS queue."""
        queue_url = (command.value or "").strip()
        if not queue_url:
            return build_error_response("Queue URL is required. Usage: sqs send <queue-url> --message <body>")

        message_body = command.args.get("message")
        if not message_body:
            return build_error_response("--message is required. Usage: sqs send <queue-url> --message <body>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("sqs", region=str(region) if region else None)

        try:
            response = client.send_message(QueueUrl=queue_url, MessageBody=str(message_body))
            return build_success_response(f"Message sent successfully. MessageId: {response.get('MessageId', '(unknown)')}")
        except Exception as exc:
            return build_error_response(f"Failed to send message: {exc}")


class _SqsReceiveProcessor(CliCommandProcessor):
    """Receives messages from an SQS queue."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "receive"

    @property
    def description(self) -> str:
        return "Receive messages from an SQS queue"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="max", description="Maximum number of messages to receive (default: 1)", required=False, type="number", aliases=["-n"], default_value="1"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Polls the specified queue for up to ``--max`` messages."""
        queue_url = (command.value or "").strip()
        if not queue_url:
            return build_error_response("Queue URL is required. Usage: sqs receive <queue-url>")

        try:
            max_messages = int(command.args.get("max", 1))
        except (ValueError, TypeError):
            max_messages = 1

        region = command.args.get("region")
        client = self._credential_manager.get_client("sqs", region=str(region) if region else None)

        try:
            response = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=max_messages)
            messages = response.get("Messages", [])

            if not messages:
                return build_response([CliServerTextOutput(value="No messages available in the queue.", style="warning")])

            return build_response([format_as_json(messages)])
        except Exception as exc:
            return build_error_response(f"Failed to receive messages: {exc}")


class _SqsPurgeProcessor(CliCommandProcessor):
    """Purges all messages from an SQS queue."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "purge"

    @property
    def description(self) -> str:
        return "Purge all messages from an SQS queue"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="dry-run", description="Show what would be purged without actually purging", required=False, type="boolean", aliases=["--dry-run"]),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Purges the queue, or previews the action in dry-run mode."""
        queue_url = (command.value or "").strip()
        if not queue_url:
            return build_error_response("Queue URL is required. Usage: sqs purge <queue-url>")

        if is_dry_run(command):
            return build_response([CliServerTextOutput(value=f"[dry-run] Would purge all messages from queue: {queue_url}", style="warning")])

        region = command.args.get("region")
        client = self._credential_manager.get_client("sqs", region=str(region) if region else None)

        try:
            client.purge_queue(QueueUrl=queue_url)
            return build_success_response(f"Queue purged successfully: {queue_url}")
        except Exception as exc:
            return build_error_response(f"Failed to purge queue: {exc}")


class AwsSqsProcessor(CliCommandProcessor):
    """Parent processor for SQS sub-commands (list, send, receive, purge)."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _SqsListProcessor(credential_manager),
            _SqsSendProcessor(credential_manager),
            _SqsReceiveProcessor(credential_manager),
            _SqsPurgeProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "sqs"

    @property
    def description(self) -> str:
        return "AWS SQS operations -- list, send, receive, purge"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Returns sub-processors for SQS operations."""
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
