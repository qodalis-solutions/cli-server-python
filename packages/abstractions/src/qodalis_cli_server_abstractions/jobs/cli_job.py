from __future__ import annotations

import abc
import asyncio

from .cli_job_execution_context import ICliJobExecutionContext


class ICliJob(abc.ABC):
    """Interface for a background job.

    Implementations should check ``cancellation_event.is_set()`` periodically
    and return early when cancellation is requested.
    """

    @abc.abstractmethod
    async def execute_async(
        self,
        context: ICliJobExecutionContext,
        cancellation_event: asyncio.Event,
    ) -> None: ...
