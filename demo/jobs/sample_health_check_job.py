from __future__ import annotations

import asyncio

from qodalis_cli_server_abstractions.jobs import ICliJob, ICliJobExecutionContext


class SampleHealthCheckJob(ICliJob):
    """A simple demo job that simulates a health check."""

    async def execute_async(
        self,
        context: ICliJobExecutionContext,
        cancellation_event: asyncio.Event,
    ) -> None:
        context.logger.info("Running health check...")
        await asyncio.sleep(0.5)
        if cancellation_event.is_set():
            context.logger.warning("Health check cancelled")
            return
        context.logger.info("Health check passed")
