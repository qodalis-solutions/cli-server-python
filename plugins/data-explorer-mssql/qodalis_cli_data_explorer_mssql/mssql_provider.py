"""MS SQL Server-backed Data Explorer provider."""

from __future__ import annotations

import asyncio
import re
import time

import pymssql

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)

# Regex to parse ADO.NET-style connection strings.
# Supports keys: Server, Database, User Id, Password, TrustServerCertificate, etc.
_CS_RE = re.compile(r"(?P<key>[^=;]+)=(?P<value>[^;]*)", re.IGNORECASE)


def _parse_connection_string(connection_string: str) -> dict[str, str]:
    """Return a lower-cased key dict from an ADO.NET connection string."""
    result: dict[str, str] = {}
    for match in _CS_RE.finditer(connection_string):
        key = match.group("key").strip().lower().replace(" ", "")
        value = match.group("value").strip()
        result[key] = value
    return result


class MssqlDataExplorerProvider(IDataExplorerProvider):
    """Executes SQL queries against a Microsoft SQL Server database.

    Connection string format (ADO.NET style)::

        Server=host,1433;Database=mydb;User Id=sa;Password=secret;TrustServerCertificate=true
    """

    def __init__(self, connection_string: str) -> None:
        self._connection_string = connection_string
        params = _parse_connection_string(connection_string)

        server_raw = params.get("server", "localhost")
        # Server may include a port as "host,port"
        if "," in server_raw:
            host_part, port_part = server_raw.split(",", 1)
            self._host = host_part.strip()
            try:
                self._port = int(port_part.strip())
            except ValueError:
                self._port = 1433
        else:
            self._host = server_raw
            self._port = 1433

        self._database = params.get("database", "master")
        self._user = params.get("userid") or params.get("uid", "sa")
        self._password = params.get("password") or params.get("pwd", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> pymssql.Connection:
        return pymssql.connect(
            server=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
        )

    def _execute_sync(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        start = time.monotonic()
        conn: pymssql.Connection | None = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(context.query, context.parameters or ())

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

    def _get_schema_sync(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        conn: pymssql.Connection | None = None
        try:
            conn = self._connect()
            cursor = conn.cursor()

            # List all user tables and views in the dbo schema
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = 'dbo' "
                "ORDER BY TABLE_NAME"
            )
            table_rows = cursor.fetchall()

            tables: list[DataExplorerSchemaTable] = []
            for table_name, table_type in table_rows:
                cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (table_name,),
                )
                col_rows = cursor.fetchall()

                # Determine primary key columns via INFORMATION_SCHEMA
                cursor.execute(
                    "SELECT kcu.COLUMN_NAME "
                    "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc "
                    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS kcu "
                    "  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                    "  AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA "
                    "  AND tc.TABLE_NAME = kcu.TABLE_NAME "
                    "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
                    "  AND tc.TABLE_SCHEMA = 'dbo' "
                    "  AND tc.TABLE_NAME = %s",
                    (table_name,),
                )
                pk_rows = cursor.fetchall()
                pk_columns = {row[0] for row in pk_rows}

                columns = [
                    DataExplorerSchemaColumn(
                        name=col_name,
                        type=data_type or "varchar",
                        nullable=is_nullable.upper() == "YES",
                        primary_key=col_name in pk_columns,
                    )
                    for col_name, data_type, is_nullable in col_rows
                ]

                # Normalise table_type to match SQLite plugin convention
                normalised_type = (
                    "view" if table_type.strip().upper() == "VIEW" else "table"
                )
                tables.append(
                    DataExplorerSchemaTable(
                        name=table_name,
                        type=normalised_type,
                        columns=columns,
                    )
                )

            return DataExplorerSchemaResult(source=options.name, tables=tables)
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # IDataExplorerProvider interface
    # ------------------------------------------------------------------

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        return await asyncio.to_thread(self._execute_sync, context)

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        return await asyncio.to_thread(self._get_schema_sync, options)
