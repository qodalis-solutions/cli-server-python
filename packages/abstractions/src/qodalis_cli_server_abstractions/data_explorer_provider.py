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
        """Execute a query against the data source.

        Args:
            context: The execution context containing the query and parameters.

        Returns:
            The query result including rows and metadata.
        """
        ...

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        """Return schema information. Override to support schema introspection."""
        return None
