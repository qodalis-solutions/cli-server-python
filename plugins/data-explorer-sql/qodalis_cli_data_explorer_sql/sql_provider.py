"""SQLite-backed Data Explorer provider."""

from __future__ import annotations

import sqlite3
import time

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)


class SqlDataExplorerProvider(IDataExplorerProvider):
    """Executes SQL queries against a SQLite database."""

    def __init__(self, filename: str = ":memory:") -> None:
        self._filename = filename

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        start = time.monotonic()
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._filename)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(context.query, context.parameters or {})

            if cursor.description:
                columns = [desc[0] for desc in cursor.description]
                rows_raw = cursor.fetchall()
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
                conn.commit()
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
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._filename)
            cursor = conn.cursor()

            cursor.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            table_rows = cursor.fetchall()

            tables: list[DataExplorerSchemaTable] = []
            for table_name, table_type in table_rows:
                cursor.execute(f'PRAGMA table_info("{table_name}")')
                col_rows = cursor.fetchall()
                columns = [
                    DataExplorerSchemaColumn(
                        name=row[1],
                        type=row[2] or "TEXT",
                        nullable=row[3] == 0,
                        primary_key=row[5] > 0,
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
                conn.close()
