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
    format_as_json,
    format_as_list,
    format_as_key_value,
    apply_output_format,
    CliServerTextOutput,
)


# ---------------------------------------------------------------------------
# dynamodb tables
# ---------------------------------------------------------------------------

class _DynamoDbTablesProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "tables"

    @property
    def description(self) -> str:
        return "List DynamoDB tables"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
            CliCommandParameterDescriptor(name="output", description="Output format (list|json|text)", required=False, type="string", aliases=["-o"], default_value="list"),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        region = command.args.get("region")
        client = self._credential_manager.get_client("dynamodb", region=str(region) if region else None)

        try:
            response = client.list_tables()
            table_names = response.get("TableNames", [])

            if not table_names:
                return build_response([CliServerTextOutput(value="No DynamoDB tables found.", style="warning")])

            list_output = format_as_list(table_names)
            return build_response([apply_output_format(command, list_output, table_names)])
        except Exception as exc:
            return build_error_response(f"Failed to list DynamoDB tables: {exc}")


# ---------------------------------------------------------------------------
# dynamodb describe
# ---------------------------------------------------------------------------

class _DynamoDbDescribeProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "describe"

    @property
    def description(self) -> str:
        return "Describe a DynamoDB table"

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
        table_name = (command.value or "").strip()
        if not table_name:
            return build_error_response("Table name is required. Usage: dynamodb describe <table-name>")

        region = command.args.get("region")
        client = self._credential_manager.get_client("dynamodb", region=str(region) if region else None)

        try:
            response = client.describe_table(TableName=table_name)
            table = response.get("Table")

            if not table:
                return build_error_response(f'Table "{table_name}" not found.')

            key_schema = ", ".join(
                f"{k.get('AttributeName', '')} ({k.get('KeyType', '')})"
                for k in table.get("KeySchema", [])
            )

            creation_dt = table.get("CreationDateTime")
            creation_str = creation_dt.isoformat() if creation_dt else "(unknown)"

            entries = {
                "TableName": table.get("TableName", "(unknown)"),
                "TableStatus": table.get("TableStatus", "(unknown)"),
                "ItemCount": str(table.get("ItemCount", 0)),
                "TableSizeBytes": str(table.get("TableSizeBytes", 0)),
                "KeySchema": key_schema,
                "CreationDateTime": creation_str,
            }

            return build_response([format_as_key_value(entries)])
        except Exception as exc:
            return build_error_response(f"Failed to describe DynamoDB table: {exc}")


# ---------------------------------------------------------------------------
# dynamodb scan
# ---------------------------------------------------------------------------

class _DynamoDbScanProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "scan"

    @property
    def description(self) -> str:
        return "Scan items from a DynamoDB table"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="limit", description="Maximum number of items to return (default: 25)", required=False, type="number", aliases=["-l"], default_value="25"),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        table_name = (command.value or "").strip()
        if not table_name:
            return build_error_response("Table name is required. Usage: dynamodb scan <table-name>")

        try:
            limit = int(command.args.get("limit", 25))
        except (ValueError, TypeError):
            limit = 25

        region = command.args.get("region")
        client = self._credential_manager.get_client("dynamodb", region=str(region) if region else None)

        try:
            response = client.scan(TableName=table_name, Limit=limit)
            items = response.get("Items", [])
            return build_response([format_as_json(items)])
        except Exception as exc:
            return build_error_response(f"Failed to scan DynamoDB table: {exc}")


# ---------------------------------------------------------------------------
# dynamodb query
# ---------------------------------------------------------------------------

class _DynamoDbQueryProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._credential_manager = credential_manager

    @property
    def command(self) -> str:
        return "query"

    @property
    def description(self) -> str:
        return "Query items from a DynamoDB table"

    @property
    def value_required(self) -> bool | None:
        return True

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(name="key", description="KeyConditionExpression for the query", required=True, type="string", aliases=["-k"]),
            CliCommandParameterDescriptor(name="filter", description="FilterExpression for the query", required=False, type="string", aliases=["-f"]),
            CliCommandParameterDescriptor(name="region", description="AWS region override", required=False, type="string", aliases=["-r"]),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""

    async def handle_structured_async(self, command: CliProcessCommand) -> Any:
        table_name = (command.value or "").strip()
        if not table_name:
            return build_error_response("Table name is required. Usage: dynamodb query <table-name> --key <expression>")

        key_condition = command.args.get("key")
        if not key_condition:
            return build_error_response("--key (KeyConditionExpression) is required. Usage: dynamodb query <table-name> --key <expression>")

        filter_expression = command.args.get("filter")

        region = command.args.get("region")
        client = self._credential_manager.get_client("dynamodb", region=str(region) if region else None)

        try:
            kwargs: dict[str, Any] = {
                "TableName": table_name,
                "KeyConditionExpression": str(key_condition),
            }
            if filter_expression:
                kwargs["FilterExpression"] = str(filter_expression)

            response = client.query(**kwargs)
            items = response.get("Items", [])
            return build_response([format_as_json(items)])
        except Exception as exc:
            return build_error_response(f"Failed to query DynamoDB table: {exc}")


# ---------------------------------------------------------------------------
# dynamodb (parent)
# ---------------------------------------------------------------------------

class AwsDynamoDbProcessor(CliCommandProcessor):
    def __init__(self, credential_manager: AwsCredentialManager) -> None:
        super().__init__()
        self._sub_processors: list[ICliCommandProcessor] = [
            _DynamoDbTablesProcessor(credential_manager),
            _DynamoDbDescribeProcessor(credential_manager),
            _DynamoDbScanProcessor(credential_manager),
            _DynamoDbQueryProcessor(credential_manager),
        ]

    @property
    def command(self) -> str:
        return "dynamodb"

    @property
    def description(self) -> str:
        return "AWS DynamoDB operations -- tables, describe, scan, query"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._sub_processors

    async def handle_async(self, command: CliProcessCommand) -> str:
        return ""
