"""The jobs plugin's entry point (``qodalis_cli.plugins``): the job scheduler (started here), its API
(``/api/v1/qcli/jobs``), the ``jobs_*`` MCP tools and the dashboard's Jobs page.  Its settings are its own ``jobs``
config provider (run history retention).  Jobs come from other plugins and the host's code; runs are kept in memory
(a storage provider needs the host's code: ``CliJobsBuilder().set_storage_provider(...)``)."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CliPluginEntryInfo

from .cli_jobs_builder import CliJobsBuilder

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="jobs", package="qodalis-cli-server-plugin-jobs", title="Jobs", icon="⏱️")


def settings() -> list[Any]:
    """No settings of its own here: the ``jobs`` config provider (run history retention) is the scheduler's."""
    return []


async def create_plugin(context: Any) -> Any:
    jobs = CliJobsBuilder().build(event_bus=context.event_bus)
    # Mounting registers its stop with the restart's graceful shutdown
    await jobs.scheduler.start()
    context.services.set("jobs", jobs)
    return jobs
