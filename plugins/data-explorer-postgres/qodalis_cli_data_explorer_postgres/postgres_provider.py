"""PostgreSQL-backed Data Explorer provider."""

from __future__ import annotations

import time

import asyncpg

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)


class PostgresDataExplorerProvider(IDataExplorerProvider):
    """Executes SQL queries against a PostgreSQL database."""

    def __init__(self, connection_string: str) -> None:
        self._connection_string = connection_string

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        start = time.monotonic()
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(self._connection_string)
            stmt = await conn.prepare(context.query)
            attributes = stmt.get_attributes()

            if attributes:
                columns = [attr.name for attr in attributes]
                rows_raw = await stmt.fetch(*( context.parameters or []))
                rows = [list(row) for row in rows_raw]
                return DataExplorerResult(
                    success=True,
                    source=context.options.name,
                    language=context.options.language,
                    default_output_format=context.options.default_output_format,
                    execution_time=int((time.monotonic() - start) * 1000),
                    columns=columns,
                    rows=rows,
                    row_count=len(rows),
                    truncated=False,
                    error=None,
                )
            else:
                status = await conn.execute(context.query, *( context.parameters or []))
                # status is a string like "INSERT 0 1" or "UPDATE 3"
                try:
                    affected = int(status.split()[-1])
                except (ValueError, IndexError):
                    affected = 0
                return DataExplorerResult(
                    success=True,
                    source=context.options.name,
                    language=context.options.language,
                    default_output_format=context.options.default_output_format,
                    execution_time=int((time.monotonic() - start) * 1000),
                    columns=["changes"],
                    rows=[[affected]],
                    row_count=1,
                    truncated=False,
                    error=None,
                )
        except Exception as exc:
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
                error=str(exc),
            )
        finally:
            if conn is not None:
                await conn.close()

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(self._connection_string)

            table_rows = await conn.fetch(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "ORDER BY table_name"
            )

            tables: list[DataExplorerSchemaTable] = []
            for table_row in table_rows:
                table_name = table_row["table_name"]
                table_type = table_row["table_type"]

                col_rows = await conn.fetch(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = $1 "
                    "ORDER BY ordinal_position",
                    table_name,
                )

                # Fetch primary key columns for this table
                pk_rows = await conn.fetch(
                    "SELECT kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON tc.constraint_name = kcu.constraint_name "
                    "  AND tc.table_schema = kcu.table_schema "
                    "WHERE tc.constraint_type = 'PRIMARY KEY' "
                    "  AND tc.table_schema = 'public' "
                    "  AND tc.table_name = $1",
                    table_name,
                )
                pk_columns = {row["column_name"] for row in pk_rows}

                columns = [
                    DataExplorerSchemaColumn(
                        name=row["column_name"],
                        type=row["data_type"],
                        nullable=row["is_nullable"] == "YES",
                        primary_key=row["column_name"] in pk_columns,
                    )
                    for row in col_rows
                ]
                tables.append(DataExplorerSchemaTable(
                    name=table_name,
                    type=table_type,
                    columns=columns,
                ))

            return DataExplorerSchemaResult(source=options.name, tables=tables)
        finally:
            if conn is not None:
                await conn.close()
