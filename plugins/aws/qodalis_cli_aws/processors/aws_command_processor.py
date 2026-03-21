from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from qodalis_cli_server_abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)

from ..services.aws_config_service import AwsConfigService
from ..services.aws_credential_manager import AwsCredentialManager
from ..utils.output_helpers import (
    build_response,
    build_error_response,
    build_success_response,
    format_as_key_value,
    format_as_list,
    CliServerTextOutput,
)
from .s3_processor import AwsS3Processor
from .ec2_processor import AwsEc2Processor
from .lambda_processor import AwsLambdaProcessor
from .cloudwatch_processor import AwsCloudWatchProcessor
from .sns_processor import AwsSnsProcessor
from .sqs_processor import AwsSqsProcessor
from .iam_processor import AwsIamProcessor
from .dynamodb_processor import AwsDynamoDbProcessor
from .ecs_processor import AwsEcsProcessor


# ---------------------------------------------------------------------------
# aws configure set
# ---------------------------------------------------------------------------

class _AwsConfigureSetProcessor(CliCommandProcessor):
    def __init__(self, config_service: AwsConfigService, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._config_service = config_service
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "set"

    @property
    def description(self) -> str:
        return "Set AWS credentials and region"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="key", description="AWS access key ID", required=False, type="string", aliases=["-k"]),
            CliCommandParameterDescriptor(name="secret", description="AWS secret access key", required=False, type="string", aliases=["-s"]),
            CliCommandParameterDescriptor(name="region", description="AWS region", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="profile", description="AWS profile name", required=False, type="string", aliases=["-p"], default_value="default"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        key = command.args.get("key")
        secret = command.args.get("secret")
        region = command.args.get("region")
        profile = command.args.get("profile")

        if key and secret:
            self._config_service.set_credentials(str(key), str(secret))
            self._credential_manager.clear_cache()
        elif key or secret:
            return build_error_response("Both --key and --secret must be provided together.")

        if region:
            self._config_service.set_region(str(region))
            self._credential_manager.clear_cache()

        if profile:
            self._config_service.set_profile(str(profile))
            self._credential_manager.clear_cache()

        if not key and not secret and not region and not profile:
            return build_error_response("Provide at least one of --key/--secret, --region, or --profile.")

        return build_success_response("AWS configuration updated.")


# ---------------------------------------------------------------------------
# aws configure get
# ---------------------------------------------------------------------------

class _AwsConfigureGetProcessor(CliCommandProcessor):
    def __init__(self, config_service: AwsConfigService) -> None:
        super().__init__()
        self._config_service = config_service

    @property
    def command(self) -> str:
        return "get"

    @property
    def description(self) -> str:
        return "Show current AWS configuration (secrets masked)"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        summary = self._config_service.get_config_summary()
        entries = {
            "Access Key ID": summary.get("access_key_id") or "(not set)",
            "Secret Access Key": summary.get("secret_access_key") or "(not set)",
            "Region": summary.get("region") or "(not set)",
            "Profile": summary.get("profile") or "default",
        }
        return build_response([format_as_key_value(entries)])


# ---------------------------------------------------------------------------
# aws configure profiles
# ---------------------------------------------------------------------------

class _AwsConfigureProfilesProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "profiles"

    @property
    def description(self) -> str:
        return "List available AWS profiles from ~/.aws/credentials and ~/.aws/config"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        profiles: set[str] = set()

        home = Path.home()
        credentials_path = home / ".aws" / "credentials"
        config_path = home / ".aws" / "config"

        self._parse_profiles(credentials_path, re.compile(r"^\[([^\]]+)\]"), profiles)
        self._parse_profiles(config_path, re.compile(r"^\[(?:profile\s+)?([^\]]+)\]"), profiles)

        items = sorted(profiles)

        if not items:
            return build_response([
                CliServerTextOutput(value="No AWS profiles found in ~/.aws/credentials or ~/.aws/config.", style="warning"),
            ])

        return build_response([format_as_list(items)])

    @staticmethod
    def _parse_profiles(file_path: Path, pattern: re.Pattern[str], profiles: set[str]) -> None:
        try:
            content = file_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                match = pattern.match(line.strip())
                if match:
                    profiles.add(match.group(1).strip())
        except (OSError, IOError):
            pass


# ---------------------------------------------------------------------------
# aws configure (parent)
# ---------------------------------------------------------------------------

class _AwsConfigureProcessor(CliCommandProcessor):
    def __init__(self, config_service: AwsConfigService, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _AwsConfigureSetProcessor(config_service, credential_manager),
            _AwsConfigureGetProcessor(config_service),
            _AwsConfigureProfilesProcessor(),
        ]

    @property
    def command(self) -> str:
        return "configure"

    @property
    def description(self) -> str:
        return "Manage AWS credentials and configuration"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""


# ---------------------------------------------------------------------------
# aws status
# ---------------------------------------------------------------------------

class _AwsStatusProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "status"

    @property
    def description(self) -> str:
        return "Test AWS connectivity using STS GetCallerIdentity"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        try:
            client = self._credential_manager.get_client("sts")
            response = client.get_caller_identity()

            entries = {
                "Account": response.get("Account", "(unknown)"),
                "Arn": response.get("Arn", "(unknown)"),
                "UserId": response.get("UserId", "(unknown)"),
            }

            return build_response([
                CliServerTextOutput(value="AWS connection successful.", style="success"),
                format_as_key_value(entries),
            ])
        except Exception as exc:
            err_name = type(exc).__name__
            err_msg = str(exc)
            if "credentials" in err_msg.lower() or err_name in ("NoCredentialsError", "PartialCredentialsError"):
                return build_error_response(
                    'No AWS credentials configured. Run "aws configure set --key <key> --secret <secret>" '
                    "or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment variables."
                )
            return build_error_response(f"AWS status check failed: {exc}")


# ---------------------------------------------------------------------------
# aws (root processor)
# ---------------------------------------------------------------------------

class AwsCommandProcessor(CliCommandProcessor):
    def __init__(self) -> None:
        super().__init__()
        self._config_service = AwsConfigService()
        self._credential_manager = AwsCredentialManager(self._config_service)
        self._sub_processors: list[ICliCommandProcessor] = [
            _AwsConfigureProcessor(self._config_service, self._credential_manager),
            _AwsStatusProcessor(self._credential_manager),
            AwsS3Processor(self._credential_manager),
            AwsEc2Processor(self._credential_manager),
            AwsLambdaProcessor(self._credential_manager),
            AwsCloudWatchProcessor(self._credential_manager),
            AwsSnsProcessor(self._credential_manager),
            AwsSqsProcessor(self._credential_manager),
            AwsEcsProcessor(self._credential_manager),
            AwsDynamoDbProcessor(self._credential_manager),
            AwsIamProcessor(self._credential_manager),
        ]

    @property
    def command(self) -> str:
        return "aws"

    @property
    def description(self) -> str:
        return "AWS cloud resource management"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._sub_processors

    def get_credential_manager(self) -> AwsCredentialManager:
        return self._credential_manager

    def get_config_service(self) -> AwsConfigService:
        return self._config_service

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
