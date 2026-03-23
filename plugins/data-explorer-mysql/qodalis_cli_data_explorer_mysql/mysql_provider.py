"""MySQL-backed Data Explorer provider."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import aiomysql

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)


class MysqlDataExplorerProvider(IDataExplorerProvider):
    """Executes SQL queries against a MySQL database."""

    def __init__(self, connection_string: str) -> None:
        parsed = urlparse(connection_string)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 3306
        self._user = parsed.username or "root"
        self._password = parsed.password or ""
        self._db = parsed.path.lstrip("/") if parsed.path else None

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        """Execute a SQL query against the MySQL database."""
        start = time.monotonic()
        conn: aiomysql.Connection | None = None
        try:
            conn = await aiomysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._db,
            )
            async with conn.cursor() as cursor:
                await cursor.execute(context.query, context.parameters or None)

                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows_raw = await cursor.fetchall()
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
                    await conn.commit()
                    return DataExplorerResult(
                        success=True,
                        source=context.options.name,
                        language=context.options.language,
                        default_output_format=context.options.default_output_format,
                        execution_time=int((time.monotonic() - start) * 1000),
                        columns=["changes"],
                        rows=[[cursor.rowcount]],
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
                conn.close()

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        """Return the database schema (tables, views, and their columns)."""
        conn: aiomysql.Connection | None = None
        try:
            conn = await aiomysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._db,
            )
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE "
                    "FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "ORDER BY TABLE_NAME"
                )
                table_rows = await cursor.fetchall()

                tables: list[DataExplorerSchemaTable] = []
                for table_name, table_type in table_rows:
                    await cursor.execute(
                        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY "
                        "FROM information_schema.columns "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION",
                        (table_name,),
                    )
                    col_rows = await cursor.fetchall()
                    columns = [
                        DataExplorerSchemaColumn(
                            name=col_name,
                            type=data_type or "TEXT",
                            nullable=is_nullable == "YES",
                            primary_key=col_key == "PRI",
                        )
                        for col_name, data_type, is_nullable, col_key in col_rows
                    ]
                    tables.append(DataExplorerSchemaTable(
                        name=table_name,
                        type=table_type,
                        columns=columns,
                    ))

                return DataExplorerSchemaResult(source=options.name, tables=tables)
        finally:
            if conn is not None:
                conn.close()
