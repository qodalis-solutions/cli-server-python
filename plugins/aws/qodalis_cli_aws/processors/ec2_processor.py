from __future__ import annotations

import asyncio

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
    format_as_key_value,
    apply_output_format,
    is_dry_run,
    CliServerTextOutput,
)


class _Ec2ListProcessor(CliCommandProcessor):
    """Lists all EC2 instances with key details."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return "List all EC2 instances"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Describes all EC2 instances and returns a summary table."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("ec2", region=str(region) if region else None)

        try:
            response = client.describe_instances()
            instances = []
            for reservation in response.get("Reservations", []):
                instances.extend(reservation.get("Instances", []))

            if not instances:
                return build_response([CliServerTextOutput(value="No instances found.", style="warning")])

            rows: list[list[str]] = []
            for inst in instances:
                name_tag = next((t["Value"] for t in inst.get("Tags", []) if t.get("Key") == "Name"), "(none)")
                rows.append([
                    inst.get("InstanceId", "(unknown)"),
                    name_tag,
                    inst.get("State", {}).get("Name", "(unknown)"),
                    inst.get("InstanceType", "(unknown)"),
                    inst.get("PublicIpAddress", "(none)"),
                ])

            table_output = format_as_table(["Instance ID", "Name", "State", "Type", "Public IP"], rows)
            return build_response([apply_output_format(command, table_output, instances)])
        except Exception as exc:
            return build_error_response(f"Failed to list instances: {exc}")


class _Ec2DescribeProcessor(CliCommandProcessor):
    """Describes a single EC2 instance in detail."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "describe"

    @property
    def description(self) -> str:
        return "Describe an EC2 instance"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Returns detailed metadata for the specified EC2 instance."""
        instance_id = (command.value or "").strip()
        if not instance_id:
            return build_error_response("Instance ID is required. Usage: ec2 describe <instance-id>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("ec2", region=str(region) if region else None)

        try:
            response = client.describe_instances(InstanceIds=[instance_id])
            instances = []
            for reservation in response.get("Reservations", []):
                instances.extend(reservation.get("Instances", []))

            if not instances:
                return build_error_response(f'Instance "{instance_id}" not found.')

            inst = instances[0]
            name_tag = next((t["Value"] for t in inst.get("Tags", []) if t.get("Key") == "Name"), "(none)")
            sg_list = ", ".join(
                f"{sg.get('GroupId', '')} ({sg.get('GroupName', '')})"
                for sg in inst.get("SecurityGroups", [])
            ) or "(none)"

            launch_time = inst.get("LaunchTime")
            launch_str = launch_time.isoformat() if launch_time else "(unknown)"

            entries = {
                "Instance ID": inst.get("InstanceId", "(unknown)"),
                "Name": name_tag,
                "State": inst.get("State", {}).get("Name", "(unknown)"),
                "Type": inst.get("InstanceType", "(unknown)"),
                "Availability Zone": inst.get("Placement", {}).get("AvailabilityZone", "(unknown)"),
                "Public IP": inst.get("PublicIpAddress", "(none)"),
                "Private IP": inst.get("PrivateIpAddress", "(none)"),
                "Launch Time": launch_str,
                "Security Groups": sg_list,
            }

            return build_response([format_as_key_value(entries)])
        except Exception as exc:
            return build_error_response(f"Failed to describe instance: {exc}")


class _Ec2StartProcessor(CliCommandProcessor):
    """Starts a stopped EC2 instance."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "start"

    @property
    def description(self) -> str:
        return "Start an EC2 instance"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Issues a start request for the specified EC2 instance."""
        instance_id = (command.value or "").strip()
        if not instance_id:
            return build_error_response("Instance ID is required. Usage: ec2 start <instance-id>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("ec2", region=str(region) if region else None)

        try:
            client.start_instances(InstanceIds=[instance_id])
            return build_success_response(f"Starting instance {instance_id}... done")
        except Exception as exc:
            return build_error_response(f"Failed to start instance: {exc}")


class _Ec2StopProcessor(CliCommandProcessor):
    """Stops a running EC2 instance."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "stop"

    @property
    def description(self) -> str:
        return "Stop an EC2 instance"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="dry-run", description="Preview without stopping", required=False, type="boolean"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Stops the specified instance, or previews the action in dry-run mode."""
        instance_id = (command.value or "").strip()
        if not instance_id:
            return build_error_response("Instance ID is required. Usage: ec2 stop <instance-id>")

        if is_dry_run(command):
            return build_response([CliServerTextOutput(value=f"[DRY RUN] Would stop instance {instance_id}", style="warning")])

        region = command.args.get("region")
        client = self._credential_manager.get_client("ec2", region=str(region) if region else None)

        try:
            client.stop_instances(InstanceIds=[instance_id])
            return build_success_response(f"Stopping instance {instance_id}... done")
        except Exception as exc:
            return build_error_response(f"Failed to stop instance: {exc}")


class _Ec2RebootProcessor(CliCommandProcessor):
    """Reboots an EC2 instance."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "reboot"

    @property
    def description(self) -> str:
        return "Reboot an EC2 instance"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="dry-run", description="Preview without rebooting", required=False, type="boolean"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Reboots the specified instance, or previews the action in dry-run mode."""
        instance_id = (command.value or "").strip()
        if not instance_id:
            return build_error_response("Instance ID is required. Usage: ec2 reboot <instance-id>")

        if is_dry_run(command):
            return build_response([CliServerTextOutput(value=f"[DRY RUN] Would reboot instance {instance_id}", style="warning")])

        region = command.args.get("region")
        client = self._credential_manager.get_client("ec2", region=str(region) if region else None)

        try:
            client.reboot_instances(InstanceIds=[instance_id])
            return build_success_response(f"Rebooting instance {instance_id}... done")
        except Exception as exc:
            return build_error_response(f"Failed to reboot instance: {exc}")


class _Ec2SgListProcessor(CliCommandProcessor):
    """Lists all EC2 security groups."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return "List all security groups"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns all EC2 security groups as a table."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("ec2", region=str(region) if region else None)

        try:
            response = client.describe_security_groups()
            groups = response.get("SecurityGroups", [])

            if not groups:
                return build_response([CliServerTextOutput(value="No security groups found.", style="warning")])

            rows = [
                [
                    sg.get("GroupId", "(unknown)"),
                    sg.get("GroupName", "(unknown)"),
                    sg.get("VpcId", "(none)"),
                    sg.get("Description", "(none)"),
                ]
                for sg in groups
            ]

            table_output = format_as_table(["Group ID", "Group Name", "VPC ID", "Description"], rows)
            return build_response([apply_output_format(command, table_output, groups)])
        except Exception as exc:
            return build_error_response(f"Failed to list security groups: {exc}")


class _Ec2SgProcessor(CliCommandProcessor):
    """Parent processor for EC2 security group sub-commands."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _Ec2SgListProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "sg"

    @property
    def description(self) -> str:
        return "EC2 security group operations"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Returns sub-processors for security group operations."""
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""


class AwsEc2Processor(CliCommandProcessor):
    """Parent processor for EC2 sub-commands (list, describe, start, stop, reboot, sg)."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _Ec2ListProcessor(credential_manager),
            _Ec2DescribeProcessor(credential_manager),
            _Ec2StartProcessor(credential_manager),
            _Ec2StopProcessor(credential_manager),
            _Ec2RebootProcessor(credential_manager),
            _Ec2SgProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "ec2"

    @property
    def description(self) -> str:
        return "Amazon EC2 operations -- manage instances and security groups"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Returns sub-processors for EC2 operations."""
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return ""
