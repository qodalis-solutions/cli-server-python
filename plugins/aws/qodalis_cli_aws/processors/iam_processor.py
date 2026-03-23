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


class _IamUsersProcessor(CliCommandProcessor):
    """Lists all IAM users."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "users"

    @property
    def description(self) -> str:
        return "List IAM users"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns all IAM users as a table."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("iam", region=str(region) if region else None)

        try:
            response = client.list_users()
            users = response.get("Users", [])

            if not users:
                return build_response([CliServerTextOutput(value="No IAM users found.", style="warning")])

            rows = [
                [
                    user.get("UserName", "(unknown)"),
                    user.get("UserId", "(unknown)"),
                    user.get("Arn", "(unknown)"),
                    user["CreateDate"].strftime("%Y-%m-%d") if user.get("CreateDate") else "(unknown)",
                ]
                for user in users
            ]

            table_output = format_as_table(["UserName", "UserId", "Arn", "CreateDate"], rows)
            return build_response([apply_output_format(command, table_output, users)])
        except Exception as exc:
            return build_error_response(f"Failed to list IAM users: {exc}")


class _IamRolesProcessor(CliCommandProcessor):
    """Lists all IAM roles."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "roles"

    @property
    def description(self) -> str:
        return "List IAM roles"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns all IAM roles as a table."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("iam", region=str(region) if region else None)

        try:
            response = client.list_roles()
            roles = response.get("Roles", [])

            if not roles:
                return build_response([CliServerTextOutput(value="No IAM roles found.", style="warning")])

            rows = [
                [
                    role.get("RoleName", "(unknown)"),
                    role.get("RoleId", "(unknown)"),
                    role.get("Arn", "(unknown)"),
                    role["CreateDate"].strftime("%Y-%m-%d") if role.get("CreateDate") else "(unknown)",
                ]
                for role in roles
            ]

            table_output = format_as_table(["RoleName", "RoleId", "Arn", "CreateDate"], rows)
            return build_response([apply_output_format(command, table_output, roles)])
        except Exception as exc:
            return build_error_response(f"Failed to list IAM roles: {exc}")


class _IamPoliciesProcessor(CliCommandProcessor):
    """Lists customer-managed IAM policies."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "policies"

    @property
    def description(self) -> str:
        return "List IAM policies (local/customer-managed)"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (table|json|text)", required=False, type="string", aliases=["-o"], default_value="table"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        """Fetches and returns customer-managed IAM policies as a table."""
        region = command.args.get("region")
        client = self._credential_manager.get_client("iam", region=str(region) if region else None)

        try:
            response = client.list_policies(Scope="Local")
            policies = response.get("Policies", [])

            if not policies:
                return build_response([CliServerTextOutput(value="No IAM policies found.", style="warning")])

            rows = [
                [
                    policy.get("PolicyName", "(unknown)"),
                    policy.get("Arn", "(unknown)"),
                    str(policy.get("AttachmentCount", 0)),
                ]
                for policy in policies
            ]

            table_output = format_as_table(["PolicyName", "Arn", "AttachmentCount"], rows)
            return build_response([apply_output_format(command, table_output, policies)])
        except Exception as exc:
            return build_error_response(f"Failed to list IAM policies: {exc}")


class AwsIamProcessor(CliCommandProcessor):
    """Parent processor for IAM sub-commands (users, roles, policies)."""

    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _IamUsersProcessor(credential_manager),
            _IamRolesProcessor(credential_manager),
            _IamPoliciesProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "iam"

    @property
    def description(self) -> str:
        return "AWS IAM operations -- users, roles, policies"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        """Returns sub-processors for IAM operations."""
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
