"""Built-in system job that prunes the job history (``jobs:prune-history``)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qodalis_cli_server_abstractions.jobs import ICliJob, ICliJobExecutionContext

if TYPE_CHECKING:
    from .cli_job_scheduler import CliJobScheduler


class PruneJobHistoryJob(ICliJob):
    """Deletes the runs that break the history retention; the run's
    ``deleted`` property holds how many.  Registered by the scheduler when
    a :class:`JobHistoryRetention` limit is set."""

    def __init__(self, scheduler: CliJobScheduler) -> None:
        self._scheduler = scheduler

    async def execute_async(
        self, context: ICliJobExecutionContext, cancellation_event: asyncio.Event
    ) -> None:
        deleted = await self._scheduler.prune_history()
        context.properties["deleted"] = deleted
        context.logger.info(f"Deleted {deleted} job execution(s)")
