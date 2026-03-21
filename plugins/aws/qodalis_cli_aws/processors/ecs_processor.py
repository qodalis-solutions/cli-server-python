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


# ---------------------------------------------------------------------------
# ecs clusters
# ---------------------------------------------------------------------------

class _EcsClustersProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "clusters"

    @property
    def description(self) -> str:
        return "List ECS clusters"

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
        client = self._credential_manager.get_client("ecs", region=str(region) if region else None)

        try:
            list_response = client.list_clusters()
            cluster_arns = list_response.get("clusterArns", [])

            if not cluster_arns:
                return build_response([CliServerTextOutput(value="No ECS clusters found.", style="warning")])

            describe_response = client.describe_clusters(clusters=cluster_arns)
            clusters = describe_response.get("clusters", [])

            rows = [
                [
                    c.get("clusterName", "(unknown)"),
                    c.get("status", "(unknown)"),
                    str(c.get("runningTasksCount", 0)),
                    str(c.get("pendingTasksCount", 0)),
                ]
                for c in clusters
            ]

            table_output = format_as_table(["Cluster Name", "Status", "Running Tasks", "Pending Tasks"], rows)
            return build_response([apply_output_format(command, table_output, clusters)])
        except Exception as exc:
            return build_error_response(f"Failed to list ECS clusters: {exc}")


# ---------------------------------------------------------------------------
# ecs services
# ---------------------------------------------------------------------------

class _EcsServicesProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "services"

    @property
    def description(self) -> str:
        return "List ECS services in a cluster"

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
        cluster = (command.value or "").strip()
        if not cluster:
            return build_error_response("Cluster name or ARN is required. Usage: ecs services <cluster>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("ecs", region=str(region) if region else None)

        try:
            list_response = client.list_services(cluster=cluster)
            service_arns = list_response.get("serviceArns", [])

            if not service_arns:
                return build_response([CliServerTextOutput(value=f'No ECS services found in cluster "{cluster}".', style="warning")])

            describe_response = client.describe_services(cluster=cluster, services=service_arns)
            services = describe_response.get("services", [])

            rows = [
                [
                    s.get("serviceName", "(unknown)"),
                    s.get("status", "(unknown)"),
                    str(s.get("desiredCount", 0)),
                    str(s.get("runningCount", 0)),
                    str(s.get("pendingCount", 0)),
                ]
                for s in services
            ]

            table_output = format_as_table(["Service Name", "Status", "Desired", "Running", "Pending"], rows)
            return build_response([apply_output_format(command, table_output, services)])
        except Exception as exc:
            return build_error_response(f"Failed to list ECS services: {exc}")


# ---------------------------------------------------------------------------
# ecs tasks
# ---------------------------------------------------------------------------

class _EcsTasksProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "tasks"

    @property
    def description(self) -> str:
        return "List ECS tasks in a cluster"

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
        cluster = (command.value or "").strip()
        if not cluster:
            return build_error_response("Cluster name or ARN is required. Usage: ecs tasks <cluster>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("ecs", region=str(region) if region else None)

        try:
            list_response = client.list_tasks(cluster=cluster)
            task_arns = list_response.get("taskArns", [])

            if not task_arns:
                return build_response([CliServerTextOutput(value=f'No ECS tasks found in cluster "{cluster}".', style="warning")])

            describe_response = client.describe_tasks(cluster=cluster, tasks=task_arns)
            tasks = describe_response.get("tasks", [])

            rows = []
            for task in tasks:
                task_arn = task.get("taskArn", "")
                task_id = task_arn.split("/")[-1] if task_arn else "(unknown)"
                task_def_arn = task.get("taskDefinitionArn", "")
                task_def = task_def_arn.split("/")[-1] if task_def_arn else "(unknown)"
                started_at = task.get("startedAt")
                started_str = started_at.strftime("%Y-%m-%d %H:%M:%S") if started_at else "(not started)"
                rows.append([task_id, task.get("lastStatus", "(unknown)"), task_def, started_str])

            table_output = format_as_table(["Task ID", "Status", "Definition", "Started"], rows)
            return build_response([apply_output_format(command, table_output, tasks)])
        except Exception as exc:
            return build_error_response(f"Failed to list ECS tasks: {exc}")


# ---------------------------------------------------------------------------
# ecs (parent)
# ---------------------------------------------------------------------------

class AwsEcsProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _EcsClustersProcessor(credential_manager),
            _EcsServicesProcessor(credential_manager),
            _EcsTasksProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "ecs"

    @property
    def description(self) -> str:
        return "AWS ECS operations -- clusters, services, tasks"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
