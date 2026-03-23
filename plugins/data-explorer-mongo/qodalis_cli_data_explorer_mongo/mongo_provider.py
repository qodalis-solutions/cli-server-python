"""MongoDB-backed Data Explorer provider."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId, json_util

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)


def _bson_to_serializable(obj: Any) -> Any:
    """Convert BSON types to JSON-serializable types."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _bson_to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bson_to_serializable(item) for item in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


class MongoDataExplorerProvider(IDataExplorerProvider):
    """Executes queries against a MongoDB database."""

    def __init__(self, connection_string: str, database: str) -> None:
        self._connection_string = connection_string
        self._database = database

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        """Execute a MongoDB query or command."""
        start = time.monotonic()
        client: AsyncIOMotorClient | None = None
        try:
            client = AsyncIOMotorClient(self._connection_string)
            db = client[self._database]
            query = context.query.strip()

            if query.lower() == "show collections":
                names = await db.list_collection_names()
                rows = [{"name": n} for n in sorted(names)]
                return self._success(context, start, None, rows, len(rows))

            if query.lower() in ("show dbs", "show databases"):
                dbs_info = await client.list_databases()
                rows = [
                    _bson_to_serializable(
                        {"name": d["name"], "sizeOnDisk": d.get("sizeOnDisk", 0), "empty": d.get("empty", False)}
                    )
                    for d in dbs_info
                ]
                return self._success(context, start, None, rows, len(rows))

            parsed = self._parse_query(query)
            if parsed is None:
                return self._error(
                    context, start,
                    "Invalid query syntax. Use: db.collection.find({...}), "
                    "db.collection.aggregate([...]), show collections, show dbs",
                )

            coll_name, operation, args = parsed
            collection = db[coll_name]
            op = operation.lower()

            if op == "find":
                filt = args[0] if args else {}
                projection = args[1] if len(args) > 1 else None
                max_rows = context.options.max_rows or 1000
                cursor = collection.find(filt, projection)
                docs = await cursor.to_list(length=max_rows + 1)
                truncated = len(docs) > max_rows
                if truncated:
                    docs.pop()
                rows = [_bson_to_serializable(d) for d in docs]
                return self._success(context, start, None, rows, len(rows), truncated)

            if op == "findone":
                filt = args[0] if args else {}
                doc = await collection.find_one(filt)
                rows = [_bson_to_serializable(doc)] if doc else []
                return self._success(context, start, None, rows, len(rows))

            if op == "aggregate":
                pipeline = args[0] if args else []
                cursor = collection.aggregate(pipeline)
                docs = await cursor.to_list(length=None)
                rows = [_bson_to_serializable(d) for d in docs]
                return self._success(context, start, None, rows, len(rows))

            if op == "insertone":
                doc = args[0] if args else {}
                result = await collection.insert_one(doc)
                rows = [{"acknowledged": result.acknowledged, "insertedId": str(result.inserted_id)}]
                return self._success(context, start, None, rows, 1)

            if op == "insertmany":
                docs = args[0] if args else []
                result = await collection.insert_many(docs)
                rows = [{"acknowledged": result.acknowledged, "insertedCount": len(result.inserted_ids)}]
                return self._success(context, start, None, rows, 1)

            if op == "updateone":
                filt = args[0] if args else {}
                update = args[1] if len(args) > 1 else {}
                result = await collection.update_one(filt, update)
                rows = [{"acknowledged": result.acknowledged, "matchedCount": result.matched_count, "modifiedCount": result.modified_count}]
                return self._success(context, start, None, rows, 1)

            if op == "updatemany":
                filt = args[0] if args else {}
                update = args[1] if len(args) > 1 else {}
                result = await collection.update_many(filt, update)
                rows = [{"acknowledged": result.acknowledged, "matchedCount": result.matched_count, "modifiedCount": result.modified_count}]
                return self._success(context, start, None, rows, 1)

            if op == "deleteone":
                filt = args[0] if args else {}
                result = await collection.delete_one(filt)
                rows = [{"acknowledged": result.acknowledged, "deletedCount": result.deleted_count}]
                return self._success(context, start, None, rows, 1)

            if op == "deletemany":
                filt = args[0] if args else {}
                result = await collection.delete_many(filt)
                rows = [{"acknowledged": result.acknowledged, "deletedCount": result.deleted_count}]
                return self._success(context, start, None, rows, 1)

            if op == "countdocuments":
                filt = args[0] if args else {}
                count = await collection.count_documents(filt)
                return self._success(context, start, ["count"], [[count]], 1)

            if op == "distinct":
                field = args[0] if args else ""
                filt = args[1] if len(args) > 1 else {}
                values = await collection.distinct(field, filt)
                rows = [{"value": _bson_to_serializable(v)} for v in values]
                return self._success(context, start, None, rows, len(rows))

            return self._error(
                context, start,
                f"Unsupported operation: {operation}. Supported: find, findOne, aggregate, "
                "insertOne, insertMany, updateOne, updateMany, deleteOne, deleteMany, countDocuments, distinct",
            )

        except Exception as exc:
            return self._error(context, start, str(exc))
        finally:
            if client is not None:
                client.close()

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        """Return collection names and inferred field schemas from sample documents."""
        client: AsyncIOMotorClient | None = None
        try:
            client = AsyncIOMotorClient(self._connection_string)
            db = client[self._database]
            coll_names = await db.list_collection_names()

            tables: list[DataExplorerSchemaTable] = []
            for coll_name in sorted(coll_names):
                sample = await db[coll_name].find_one()
                columns: list[DataExplorerSchemaColumn] = []
                if sample:
                    for key, value in sample.items():
                        if isinstance(value, list):
                            col_type = "array"
                        elif isinstance(value, dict):
                            col_type = "object"
                        elif value is None:
                            col_type = "null"
                        else:
                            col_type = type(value).__name__
                        columns.append(DataExplorerSchemaColumn(
                            name=key,
                            type=col_type,
                            nullable=True,
                            primary_key=key == "_id",
                        ))
                tables.append(DataExplorerSchemaTable(
                    name=coll_name,
                    type="collection",
                    columns=columns,
                ))

            return DataExplorerSchemaResult(source=options.name, tables=tables)
        finally:
            if client is not None:
                client.close()

    def _parse_query(self, query: str) -> tuple[str, str, list[Any]] | None:
        """Parse db.collection.operation(...) syntax."""
        match = re.match(r"^db\.(\w+)\.(\w+)\(([\s\S]*)\)$", query)
        if not match:
            return None

        collection = match.group(1)
        operation = match.group(2)
        args_str = match.group(3).strip()

        if not args_str:
            return collection, operation, []

        try:
            args = self._split_and_parse_args(args_str)
            return collection, operation, args
        except Exception:
            return None

    def _split_and_parse_args(self, args_str: str) -> list[Any]:
        """Split comma-separated top-level arguments and parse each as JSON."""
        args: list[str] = []
        depth = 0
        current: list[str] = []

        for c in args_str:
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
            elif c == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue
            current.append(c)

        if current:
            args.append("".join(current).strip())

        return [json.loads(a) for a in args]

    def _success(
        self,
        context: DataExplorerExecutionContext,
        start: float,
        columns: list[str] | None,
        rows: list[Any],
        row_count: int,
        truncated: bool = False,
    ) -> DataExplorerResult:
        """Build a successful DataExplorerResult."""
        return DataExplorerResult(
            success=True,
            source=context.options.name,
            language=context.options.language,
            default_output_format=context.options.default_output_format,
            execution_time=int((time.monotonic() - start) * 1000),
            columns=columns,
            rows=rows,
            row_count=row_count,
            truncated=truncated,
            error=None,
        )

    def _error(
        self,
        context: DataExplorerExecutionContext,
        start: float,
        error: str,
    ) -> DataExplorerResult:
        """Build a failed DataExplorerResult with the given error message."""
        return DataExplorerResult(
            success=False,
            source=context.options.name,
            language=context.options.language,
            default_output_format=context.options.default_output_format,
            execution_time=int((time.monotonic() - start) * 1000),
            columns=None,
            rows=[],
            row_count=0,
            truncated=False,
            error=error,
        )
