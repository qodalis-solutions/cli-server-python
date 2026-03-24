from __future__ import annotations

import asyncio

import json
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
    format_as_table,
    format_as_json,
    apply_output_format,
    CliServerTextOutput,
)


class _LambdaListProcessor(CliCommandProcessor):
    """Lists all Lambda functions with runtime and memory details."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return "List Lambda functions"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns all Lambda functions as a table."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("lambda", region=str(region) if region else None)

        try:
            response = client.list_functions()
            functions = response.get("Functions", [])

            if not functions:
                return build_response([CliServerTextOutput(value="No Lambda functions found.", style="warning")])

            rows = [
                [
                    fn.get("FunctionName", "(unknown)"),
                    fn.get("Runtime", "(unknown)"),
                    str(fn.get("MemorySize", "(unknown)")),
                    fn.get("LastModified", "(unknown)"),
                ]
                for fn in functions
            ]

            table_output = format_as_table(["Name", "Runtime", "Memory (MB)", "Last Modified"], rows)
            return build_response([apply_output_format(command, table_output, functions)])
        except Exception as exc:
            return build_error_response(f"Failed to list Lambda functions: {exc}")


class _LambdaInvokeProcessor(CliCommandProcessor):
    """Invokes a Lambda function and returns the result."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "invoke"

    @property
    def description(self) -> str:
        return "Invoke a Lambda function"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="payload", description="JSON payload to send to the function", required=False, type="string", aliases=["-p"]),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Invokes the specified Lambda function with an optional JSON payload."""
        function_name = (command.value or "").strip()
        if not function_name:
            return build_error_response("Function name is required. Usage: lambda invoke <function-name>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("lambda", region=str(region) if region else None)

        payload_str = command.args.get("payload")

        try:
            params: dict[str, Any] = {"FunctionName": function_name}
            if payload_str:
                params["Payload"] = str(payload_str).encode("utf-8")

            response = client.invoke(**params)

            if response.get("FunctionError"):
                error_body = response["Payload"].read().decode("utf-8") if response.get("Payload") else "(no details)"
                return build_error_response(f"Function error ({response['FunctionError']}): {error_body}")

            result_body = response["Payload"].read().decode("utf-8") if response.get("Payload") else "null"

            try:
                parsed = json.loads(result_body)
            except (json.JSONDecodeError, ValueError):
                parsed = result_body

            return build_response([
                format_as_json({
                    "StatusCode": response.get("StatusCode"),
                    "ExecutedVersion": response.get("ExecutedVersion", "(unknown)"),
                    "Result": parsed,
                }),
            ])
        except Exception as exc:
            return build_error_response(f"Failed to invoke function: {exc}")


class _LambdaLogsProcessor(CliCommandProcessor):
    """Fetches recent CloudWatch logs for a Lambda function."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "logs"

    @property
    def description(self) -> str:
        return "View recent logs for a Lambda function"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="limit", description="Maximum number of log events (default: 50)", required=False, type="number", aliases=["-l"], default_value="50"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Queries the ``/aws/lambda/<name>`` log group for recent events."""
        function_name = (command.value or "").strip()
        if not function_name:
            return build_error_response("Function name is required. Usage: lambda logs <function-name>")

        try:
            limit = int(command.args.get("limit", 50))
        except (ValueError, TypeError):
            return build_error_response("--limit must be a positive number.")

        if limit <= 0:
            return build_error_response("--limit must be a positive number.")

        region = command.args.get("region")
        client = self._credential_manager.get_client("logs", region=str(region) if region else None)

        log_group_name = f"/aws/lambda/{function_name}"

        try:
            response = client.filter_log_events(logGroupName=log_group_name, limit=limit)
            events = response.get("events", [])

            if not events:
                return build_response([CliServerTextOutput(value=f"No log events found for {log_group_name}.", style="warning")])

            from datetime import datetime, timezone

            lines = []
            for event in events:
                ts = event.get("timestamp")
                if ts:
                    timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    timestamp = "(unknown)"
                message = (event.get("message") or "").rstrip()
                lines.append(f"{timestamp}  {message}")

            return build_response([CliServerTextOutput(value="\n".join(lines))])
        except Exception as exc:
            return build_error_response(f"Failed to fetch logs: {exc}")


class AwsLambdaProcessor(CliCommandProcessor):
    """Parent processor for Lambda sub-commands (list, invoke, logs)."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _LambdaListProcessor(credential_manager),
            _LambdaInvokeProcessor(credential_manager),
            _LambdaLogsProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "lambda"

    @property
    def description(self) -> str:
        return "AWS Lambda operations -- list, invoke, and view logs"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Returns sub-processors for Lambda operations."""
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""
