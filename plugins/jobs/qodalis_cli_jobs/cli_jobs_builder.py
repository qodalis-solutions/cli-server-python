"""Fluent builder for configuring and creating the jobs plugin."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from fastapi import APIRouter

from qodalis_cli_server_abstractions.dashboard_modules import DASHBOARD_MODULES_PREFIX, module_page_of
from qodalis_cli_server_abstractions.events import ICliEventBus
from qodalis_cli_server_abstractions.jobs import (
    CliJobOptions,
    ICliJob,
    ICliJobStorageProvider,
    JobHistoryRetention,
)

from .cli_job_scheduler import CliJobScheduler, BroadcastFn
from .cli_jobs_controller import create_cli_jobs_router
from .in_memory_job_storage_provider import InMemoryJobStorageProvider


#: The dashboard's Jobs page (``@qodalis/cli-dashboard-module-jobs``): its registration, served at
#: ``/qcli/modules/jobs`` with cards on the Overview, entries in the command palette (jump to a job) and a search.
JOBS_DASHBOARD_PAGE = {
    "id": "jobs",
    "title": "Jobs",
    "titles": {
        "es": "Tareas", "fr": "Tâches", "de": "Jobs", "pt": "Tarefas", "it": "Job",
        "ja": "ジョブ", "ko": "작업", "zh": "任务", "ru": "Задания", "ro": "Joburi",
    },
    "icon": "⏱",
    "group": "operations",
    "order": 10,
    "permission": "jobs:read",
    "widgets": ["overview", "palette"],
    "search": "/?q=",
}


@dataclass
class CliJobsPlugin:
    """Result of building the jobs plugin — contains the router and scheduler."""

    prefix: str
    router: APIRouter
    scheduler: CliJobScheduler
    #: The dashboard's Jobs page, when the package carries its build (mounted with the plugin).
    dashboard_pages: list[Any] = field(default_factory=list)

    def on_mount(self, server: object) -> None:
        """Hook called by ``CliServerResult.mount_plugin``: publishes the
        ``job:*`` events on the server's event bus (unless built with one) and
        registers the ``jobs_*`` MCP tools on the server's MCP tool registry."""
        bus = getattr(server, "event_bus", None)
        if self.scheduler.event_bus is None and bus is not None:
            self.scheduler.event_bus = bus
        mcp = getattr(server, "mcp", None)
        if mcp is not None and hasattr(mcp, "add_provider"):
            from .mcp_tools import JobsMcpToolProvider

            mcp.add_provider(JobsMcpToolProvider(self.scheduler), builtin=True)


class CliJobsBuilder:
    """Fluent builder for configuring and creating the jobs plugin."""

    def __init__(self) -> None:
        self._registrations: list[tuple[ICliJob, CliJobOptions]] = []
        self._storage_provider: ICliJobStorageProvider | None = None
        self._history_retention: JobHistoryRetention | None = None
        self._dashboard_dir: str | bool | None = None

    def set_dashboard_dir(self, directory: str | bool) -> CliJobsBuilder:
        """Where the dashboard's Jobs page is (``@qodalis/cli-dashboard-module-jobs``'s build); by default the
        copy in this package (``scripts/copy_dashboard.py``). ``False`` leaves the page out."""
        self._dashboard_dir = directory
        return self

    def add_job(self, job: ICliJob, options: CliJobOptions) -> CliJobsBuilder:
        """Register a background job with the given options."""
        self._registrations.append((job, options))
        return self

    def set_storage_provider(self, provider: ICliJobStorageProvider) -> CliJobsBuilder:
        """Set a custom storage provider for job execution history."""
        self._storage_provider = provider
        return self

    def set_history_retention(self, retention: JobHistoryRetention) -> CliJobsBuilder:
        """Keep the job history bounded (off by default): runs older than
        ``max_age`` or beyond the newest ``max_runs_per_job`` of their job are
        deleted every ``prune_interval`` by the system job
        ``jobs:prune-history`` (also registered when only a job's
        ``CliJobOptions.history`` sets limits).  A job's ``history`` overrides
        these limits for that job; runs of jobs no longer registered follow
        them."""
        retention.validate()
        self._history_retention = retention
        return self

    def build(
        self,
        broadcast_fn: BroadcastFn | None = None,
        event_bus: ICliEventBus | None = None,
    ) -> CliJobsPlugin:
        """Build the plugin, returning the router and scheduler.

        ``job:*`` events go to ``event_bus`` (default: the server's, set when
        mounted); ``broadcast_fn`` (JSON strings to every socket) is only used
        without a bus."""
        storage = self._storage_provider or InMemoryJobStorageProvider()
        scheduler = CliJobScheduler(
            storage,
            broadcast_fn=broadcast_fn,
            event_bus=event_bus,
            history_retention=self._history_retention,
        )

        for job, opts in self._registrations:
            scheduler.register(job, opts)
        scheduler.ensure_prune_history_job()

        router = create_cli_jobs_router(scheduler, storage)

        page = module_page_of(
            os.path.dirname(os.path.abspath(__file__)), self._dashboard_dir,
            f"{DASHBOARD_MODULES_PREFIX}/jobs", JOBS_DASHBOARD_PAGE,
        )
        return CliJobsPlugin(
            prefix="/api/v1/qcli/jobs", router=router, scheduler=scheduler,
            dashboard_pages=[page] if page else [],
        )
