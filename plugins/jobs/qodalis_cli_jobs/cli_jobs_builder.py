"""Fluent builder for configuring and creating the jobs plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Awaitable

from fastapi import APIRouter

from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobStorageProvider,
)

from .cli_job_scheduler import CliJobScheduler, BroadcastFn
from .cli_jobs_controller import create_cli_jobs_router
from .in_memory_job_storage_provider import InMemoryJobStorageProvider


@dataclass
class CliJobsPlugin:
    """Result of building the jobs plugin — contains the router and scheduler."""

    prefix: str
    router: APIRouter
    scheduler: CliJobScheduler


class CliJobsBuilder:
    """Fluent builder for configuring and creating the jobs plugin."""

    def __init__(self) -> None:
        self._registrations: list[tuple[ICliJob, CliJobOptions]] = []
        self._storage_provider: ICliJobStorageProvider | None = None

    def add_job(self, job: ICliJob, options: CliJobOptions) -> CliJobsBuilder:
        """Register a background job with the given options."""
        self._registrations.append((job, options))
        return self

    def set_storage_provider(self, provider: ICliJobStorageProvider) -> CliJobsBuilder:
        """Set a custom storage provider for job execution history."""
        self._storage_provider = provider
        return self

    def build(self, broadcast_fn: BroadcastFn | None = None) -> CliJobsPlugin:
        """Build the plugin, returning the router and scheduler."""
        storage = self._storage_provider or InMemoryJobStorageProvider()
        scheduler = CliJobScheduler(storage, broadcast_fn=broadcast_fn)

        for job, opts in self._registrations:
            scheduler.register(job, opts)

        router = create_cli_jobs_router(scheduler, storage)

        return CliJobsPlugin(prefix="/api/v1/qcli/jobs", router=router, scheduler=scheduler)
