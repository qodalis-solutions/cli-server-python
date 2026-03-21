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
    format_as_table,
    format_as_list,
    apply_output_format,
    CliServerTextOutput,
)


# ---------------------------------------------------------------------------
# sns topics
# ---------------------------------------------------------------------------

class _SnsTopicsProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "topics"

    @property
    def description(self) -> str:
        return "List SNS topics"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        region = command.args.get("region")
        client = self._credential_manager.get_client("sns", region=str(region) if region else None)

        try:
            response = client.list_topics()
            topics = response.get("Topics", [])

            if not topics:
                return build_response([CliServerTextOutput(value="No SNS topics found.", style="warning")])

            arns = [t.get("TopicArn", "(unknown)") for t in topics]
            list_output = format_as_list(arns)
            return build_response([apply_output_format(command, list_output, topics)])
        except Exception as exc:
            return build_error_response(f"Failed to list SNS topics: {exc}")


# ---------------------------------------------------------------------------
# sns publish
# ---------------------------------------------------------------------------

class _SnsPublishProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "publish"

    @property
    def description(self) -> str:
        return "Publish a message to an SNS topic"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="message", description="Message to publish", required=True, type="string", aliases=["-m"]),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        topic_arn = (command.value or "").strip()
        if not topic_arn:
            return build_error_response("Topic ARN is required. Usage: sns publish <topic-arn> --message <message>")

        message = command.args.get("message")
        if not message:
            return build_error_response("--message is required. Usage: sns publish <topic-arn> --message <message>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("sns", region=str(region) if region else None)

        try:
            response = client.publish(TopicArn=topic_arn, Message=str(message))
            return build_success_response(f"Message published successfully. MessageId: {response.get('MessageId', '(unknown)')}")
        except Exception as exc:
            return build_error_response(f"Failed to publish message: {exc}")


# ---------------------------------------------------------------------------
# sns subscriptions
# ---------------------------------------------------------------------------

class _SnsSubscriptionsProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "subscriptions"

    @property
    def description(self) -> str:
        return "List SNS subscriptions (optionally filtered by topic ARN)"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        topic_arn = (command.value or "").strip() or None

        region = command.args.get("region")
        client = self._credential_manager.get_client("sns", region=str(region) if region else None)

        try:
            if topic_arn:
                response = client.list_subscriptions_by_topic(TopicArn=topic_arn)
            else:
                response = client.list_subscriptions()

            subscriptions = response.get("Subscriptions", [])

            if not subscriptions:
                return build_response([CliServerTextOutput(value="No SNS subscriptions found.", style="warning")])

            rows = [
                [
                    sub.get("SubscriptionArn", "(unknown)"),
                    sub.get("Protocol", "(unknown)"),
                    sub.get("Endpoint", "(unknown)"),
                    sub.get("TopicArn", "(unknown)"),
                ]
                for sub in subscriptions
            ]

            table_output = format_as_table(["SubscriptionArn", "Protocol", "Endpoint", "TopicArn"], rows)
            return build_response([apply_output_format(command, table_output, subscriptions)])
        except Exception as exc:
            return build_error_response(f"Failed to list SNS subscriptions: {exc}")


# ---------------------------------------------------------------------------
# sns (parent)
# ---------------------------------------------------------------------------

class AwsSnsProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _SnsTopicsProcessor(credential_manager),
            _SnsPublishProcessor(credential_manager),
            _SnsSubscriptionsProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "sns"

    @property
    def description(self) -> str:
        return "AWS SNS operations -- topics, publish, subscriptions"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
