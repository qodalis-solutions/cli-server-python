"""Abstract base class for Data Explorer providers."""

from __future__ import annotations

import abc

from .data_explorer_types import DataExplorerExecutionContext, DataExplorerResult


class IDataExplorerProvider(abc.ABC):
    """Interface for data explorer providers that execute queries against a
    data source and return structured results."""

    @abc.abstractmethod
    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        ...
