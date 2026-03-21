"""Abstract base class for Data Explorer providers."""

from __future__ import annotations

import abc

from .data_explorer_types import (
    DataExplorerExecutionContext,
    DataExplorerResult,
    DataExplorerProviderOptions,
    DataExplorerSchemaResult,
)


class IDataExplorerProvider(abc.ABC):
    """Interface for data explorer providers that execute queries against a
    data source and return structured results."""

    @abc.abstractmethod
    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        ...

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        """Return schema information. Override to support schema introspection."""
        return None
