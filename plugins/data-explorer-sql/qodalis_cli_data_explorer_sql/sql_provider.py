"""SQLite-backed Data Explorer provider."""

from __future__ import annotations

import sqlite3
import time

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerResult,
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
