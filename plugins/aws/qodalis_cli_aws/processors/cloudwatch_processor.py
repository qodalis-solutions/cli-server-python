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
    format_as_table,
    apply_output_format,
    CliServerTextOutput,
)


class _CloudWatchAlarmsProcessor(CliCommandProcessor):
    """Lists CloudWatch metric alarms."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "alarms"

    @property
    def description(self) -> str:
        return "List CloudWatch alarms"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns all CloudWatch metric alarms."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("cloudwatch", region=str(region) if region else None)

        try:
            response = client.describe_alarms()
            alarms = response.get("MetricAlarms", [])

            if not alarms:
                return build_response([CliServerTextOutput(value="No alarms found.", style="warning")])

            rows = [
                [
                    a.get("AlarmName", "(unknown)"),
                    a.get("StateValue", "(unknown)"),
                    a.get("MetricName", "(unknown)"),
                    a.get("Namespace", "(unknown)"),
                ]
                for a in alarms
            ]

            table_output = format_as_table(["Name", "State", "Metric", "Namespace"], rows)
            return build_response([apply_output_format(command, table_output, alarms)])
        except Exception as exc:
            return build_error_response(f"Failed to list alarms: {exc}")


class _CloudWatchLogsProcessor(CliCommandProcessor):
    """Fetches log events from a CloudWatch log group."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "logs"

    @property
    def description(self) -> str:
        return "Fetch log events from a CloudWatch log group"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="filter", description="Filter pattern for log events", required=False, type="string", aliases=["-f"]),
            CliCommandParameterDescriptor(name="limit", description="Maximum number of events to return (default: 50)", required=False, type="number", aliases=["-l"], default_value="50"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Retrieves and formats recent log events from the specified log group."""
        log_group_name = (command.value or "").strip()
        if not log_group_name:
            return build_error_response("Log group name is required. Usage: cloudwatch logs <log-group>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("logs", region=str(region) if region else None)

        try:
            limit = int(command.args.get("limit", 50))
        except (ValueError, TypeError):
            limit = 50

        filter_pattern = command.args.get("filter")

        try:
            kwargs: dict[str, Any] = {"logGroupName": log_group_name, "limit": limit}
            if filter_pattern:
                kwargs["filterPattern"] = str(filter_pattern)

            response = client.filter_log_events(**kwargs)
            events = response.get("events", [])

            if not events:
                return build_response([CliServerTextOutput(value="No log events found.", style="warning")])

            from datetime import datetime, timezone

            lines = []
            for e in events:
                ts = e.get("timestamp")
                if ts:
                    timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                else:
                    timestamp = "(unknown)"
                message = (e.get("message") or "").rstrip()
                lines.append(f"{timestamp} {message}")

            return build_response([CliServerTextOutput(value="\n".join(lines))])
        except Exception as exc:
            return build_error_response(f"Failed to fetch log events: {exc}")


class _CloudWatchMetricsProcessor(CliCommandProcessor):
    """Lists CloudWatch metrics for a given namespace."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "metrics"

    @property
    def description(self) -> str:
        return "List CloudWatch metrics for a namespace"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Queries metrics for the specified CloudWatch namespace."""
        namespace = (command.value or "").strip()
        if not namespace:
            return build_error_response("Namespace is required. Usage: cloudwatch metrics <namespace>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("cloudwatch", region=str(region) if region else None)

        try:
            response = client.list_metrics(Namespace=namespace)
            metrics = response.get("Metrics", [])

            if not metrics:
                return build_response([CliServerTextOutput(value=f'No metrics found for namespace "{namespace}".', style="warning")])

            rows = [
                [
                    m.get("MetricName", "(unknown)"),
                    ", ".join(f"{d.get('Name', '')}={d.get('Value', '')}" for d in m.get("Dimensions", [])),
                ]
                for m in metrics
            ]

            table_output = format_as_table(["MetricName", "Dimensions"], rows)
            return build_response([apply_output_format(command, table_output, metrics)])
        except Exception as exc:
            return build_error_response(f"Failed to list metrics: {exc}")


class AwsCloudWatchProcessor(CliCommandProcessor):
    """Parent processor for CloudWatch sub-commands (alarms, logs, metrics)."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _CloudWatchAlarmsProcessor(credential_manager),
            _CloudWatchLogsProcessor(credential_manager),
            _CloudWatchMetricsProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "cloudwatch"

    @property
    def description(self) -> str:
        return "Amazon CloudWatch -- alarms, logs, and metrics"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Returns sub-processors for alarms, logs, and metrics."""
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
