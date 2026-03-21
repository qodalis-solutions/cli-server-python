from __future__ import annotations

import re
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
    format_as_list,
    format_as_table,
    apply_output_format,
    is_dry_run,
    CliServerTextOutput,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_s3_uri(uri: str) -> tuple[str, str] | None:
    match = re.match(r"^s3://([^/]+)/?(.*)$", uri)
    if not match:
        return None
    return match.group(1), match.group(2)


def _format_bytes(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    value = float(num_bytes)
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}"


# ---------------------------------------------------------------------------
# s3 ls
# ---------------------------------------------------------------------------

class _S3LsProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "ls"

    @property
    def description(self) -> str:
        return "List S3 buckets or objects in a bucket"

    @property
    def value_required(self) -> bool | None:
        return False

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
        client = self._credential_manager.get_client("s3", region=str(region) if region else None)

        value = (command.value or "").strip()

        if not value:
            try:
                response = client.list_buckets()
                buckets = response.get("Buckets", [])

                if not buckets:
                    return build_response([CliServerTextOutput(value="No buckets found.", style="warning")])

                items = []
                for b in buckets:
                    date = b["CreationDate"].strftime("%Y-%m-%d") if b.get("CreationDate") else "(unknown)"
                    items.append(f"{date}  {b.get('Name', '(unknown)')}")

                default_output = format_as_list(items)
                return build_response([apply_output_format(command, default_output, buckets)])
            except Exception as exc:
                return build_error_response(f"Failed to list buckets: {exc}")

        parsed = _parse_s3_uri(value)
        if not parsed:
            return build_error_response(f'Invalid S3 URI: "{value}". Expected format: s3://bucket[/prefix]')

        bucket, prefix = parsed

        try:
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix or "")

            objects: list[dict[str, Any]] = []
            for page in pages:
                objects.extend(page.get("Contents", []))

            if not objects:
                return build_response([CliServerTextOutput(value=f"No objects found in s3://{bucket}/{prefix}", style="warning")])

            rows: list[list[str]] = []
            for obj in objects:
                date = obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S") if obj.get("LastModified") else "(unknown)"
                size = _format_bytes(obj.get("Size", 0))
                rows.append([date, size, obj.get("Key", "")])

            table_output = format_as_table(["Last Modified", "Size", "Key"], rows)
            return build_response([apply_output_format(command, table_output, objects)])
        except Exception as exc:
            return build_error_response(f"Failed to list objects: {exc}")


# ---------------------------------------------------------------------------
# s3 cp
# ---------------------------------------------------------------------------

class _S3CpProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "cp"

    @property
    def description(self) -> str:
        return "Copy objects between S3 locations (S3-to-S3 only)"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="dest", description="Destination S3 URI (s3://bucket/key)", required=True, type="string", aliases=["-d"]),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        source = (command.value or "").strip()
        dest = command.args.get("dest")

        if not source:
            return build_error_response("Source S3 URI is required. Usage: s3 cp <s3://bucket/key> --dest <s3://bucket/key>")

        if not dest:
            return build_error_response("Destination is required. Use --dest <s3://bucket/key>.")

        src_parsed = _parse_s3_uri(source)
        if not src_parsed or not src_parsed[1]:
            return build_error_response(f'Invalid source S3 URI: "{source}". Expected format: s3://bucket/key')

        dst_parsed = _parse_s3_uri(str(dest))
        if not dst_parsed or not dst_parsed[1]:
            return build_error_response(f'Invalid destination S3 URI: "{dest}". Expected format: s3://bucket/key')

        region = command.args.get("region")
        client = self._credential_manager.get_client("s3", region=str(region) if region else None)

        try:
            client.copy_object(
                Bucket=dst_parsed[0],
                Key=dst_parsed[1],
                CopySource=f"{src_parsed[0]}/{src_parsed[1]}",
            )
            return build_success_response(f"Copied {source} to {dest}")
        except Exception as exc:
            return build_error_response(f"Failed to copy object: {exc}")


# ---------------------------------------------------------------------------
# s3 rm
# ---------------------------------------------------------------------------

class _S3RmProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "rm"

    @property
    def description(self) -> str:
        return "Delete an S3 object"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="dry-run", description="Preview without deleting", required=False, type="boolean"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        value = (command.value or "").strip()
        if not value:
            return build_error_response("S3 URI is required. Usage: s3 rm <s3://bucket/key>")

        parsed = _parse_s3_uri(value)
        if not parsed or not parsed[1]:
            return build_error_response(f'Invalid S3 URI: "{value}". Expected format: s3://bucket/key')

        if is_dry_run(command):
            return build_response([CliServerTextOutput(value=f"[DRY RUN] Would delete {value}", style="warning")])

        region = command.args.get("region")
        client = self._credential_manager.get_client("s3", region=str(region) if region else None)

        try:
            client.delete_object(Bucket=parsed[0], Key=parsed[1])
            return build_success_response(f"Deleted {value}")
        except Exception as exc:
            return build_error_response(f"Failed to delete object: {exc}")


# ---------------------------------------------------------------------------
# s3 mb (make bucket)
# ---------------------------------------------------------------------------

class _S3MbProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "mb"

    @property
    def description(self) -> str:
        return "Create an S3 bucket"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        bucket_name = (command.value or "").strip()
        if not bucket_name:
            return build_error_response("Bucket name is required. Usage: s3 mb <bucket-name>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("s3", region=str(region) if region else None)

        try:
            client.create_bucket(Bucket=bucket_name)
            return build_success_response(f'Bucket "{bucket_name}" created successfully.')
        except Exception as exc:
            return build_error_response(f"Failed to create bucket: {exc}")


# ---------------------------------------------------------------------------
# s3 rb (remove bucket)
# ---------------------------------------------------------------------------

class _S3RbProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "rb"

    @property
    def description(self) -> str:
        return "Delete an S3 bucket"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="dry-run", description="Preview without deleting", required=False, type="boolean"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        bucket_name = (command.value or "").strip()
        if not bucket_name:
            return build_error_response("Bucket name is required. Usage: s3 rb <bucket-name>")

        if is_dry_run(command):
            return build_response([CliServerTextOutput(value=f'[DRY RUN] Would delete bucket "{bucket_name}"', style="warning")])

        region = command.args.get("region")
        client = self._credential_manager.get_client("s3", region=str(region) if region else None)

        try:
            client.delete_bucket(Bucket=bucket_name)
            return build_success_response(f'Bucket "{bucket_name}" deleted successfully.')
        except Exception as exc:
            return build_error_response(f"Failed to delete bucket: {exc}")


# ---------------------------------------------------------------------------
# s3 presign
# ---------------------------------------------------------------------------

class _S3PresignProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "presign"

    @property
    def description(self) -> str:
        return "Generate a pre-signed URL for an S3 object"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="expires", description="URL expiration in seconds (default: 3600)", required=False, type="number", aliases=["-e"], default_value="3600"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        value = (command.value or "").strip()
        if not value:
            return build_error_response("S3 URI is required. Usage: s3 presign <s3://bucket/key>")

        parsed = _parse_s3_uri(value)
        if not parsed or not parsed[1]:
            return build_error_response(f'Invalid S3 URI: "{value}". Expected format: s3://bucket/key')

        try:
            expires_in = int(command.args.get("expires", 3600))
        except (ValueError, TypeError):
            return build_error_response("--expires must be a positive number of seconds.")

        if expires_in <= 0:
            return build_error_response("--expires must be a positive number of seconds.")

        region = command.args.get("region")
        client = self._credential_manager.get_client("s3", region=str(region) if region else None)

        try:
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": parsed[0], "Key": parsed[1]},
                ExpiresIn=expires_in,
            )
            return build_response([
                CliServerTextOutput(value=url),
                CliServerTextOutput(value=f"Expires in {expires_in} seconds.", style="info"),
            ])
        except Exception as exc:
            return build_error_response(f"Failed to generate pre-signed URL: {exc}")


# ---------------------------------------------------------------------------
# s3 (parent)
# ---------------------------------------------------------------------------

class AwsS3Processor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _S3LsProcessor(credential_manager),
            _S3CpProcessor(credential_manager),
            _S3RmProcessor(credential_manager),
            _S3MbProcessor(credential_manager),
            _S3RbProcessor(credential_manager),
            _S3PresignProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "s3"

    @property
    def description(self) -> str:
        return "Amazon S3 operations -- list, copy, remove objects and buckets"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
