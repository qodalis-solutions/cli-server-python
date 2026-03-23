"""Executor that dispatches queries to the correct Data Explorer provider."""

from __future__ import annotations

import asyncio
from typing import Any

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerResult,
)

from .data_explorer_registry import DataExplorerRegistry


class DataExplorerExecutor:
    """Wraps provider execution with timeout enforcement and row truncation."""

    def __init__(self, registry: DataExplorerRegistry) -> None:
        self._registry = registry

    async def execute_async(self, request: dict[str, Any]) -> DataExplorerResult:
        """Execute a query against the appropriate provider with timeout enforcement."""
        source = request.get("source", "")
        query = request.get("query", "")
        parameters = request.get("parameters") or {}

        entry = self._registry.get(source)
        if entry is None:
            return DataExplorerResult(
                success=False,
                source=source,
                language="sql",  # type: ignore[arg-type]
                default_output_format="table",  # type: ignore[arg-type]
                execution_time=0,
                columns=None,
                rows=[],
                row_count=0,
                truncated=False,
                error=f"Unknown data source: {source}",
            )

        provider, options = entry
        context = DataExplorerExecutionContext(
            query=query,
            parameters=parameters,
            options=options,
        )

        timeout_s = options.timeout / 1000.0

        try:
            result = await asyncio.wait_for(
                provider.execute_async(context),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return DataExplorerResult(
                success=False,
                source=source,
                language=options.language,
                default_output_format=options.default_output_format,
                execution_time=options.timeout,
                columns=None,
                rows=[],
                row_count=0,
                truncated=False,
                error=f"Query timed out after {options.timeout}ms",
            )
        except Exception as exc:
            return DataExplorerResult(
                success=False,
                source=source,
                language=options.language,
                default_output_format=options.default_output_format,
                execution_time=0,
                columns=None,
                rows=[],
                row_count=0,
                truncated=False,
                error=str(exc),
            )

        if result.row_count > options.max_rows:
            result.rows = result.rows[: options.max_rows]
            result.row_count = options.max_rows
            result.truncated = True

        return result
