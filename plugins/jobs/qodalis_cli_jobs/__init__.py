"""Qodalis CLI jobs plugin — background job scheduler."""

#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "⏱️"

from .cli_job_execution_context import CliJobExecutionContext
from .cli_job_logger import CliJobLogger
from .cli_job_scheduler import (
    BroadcastFn,
    CliJobScheduler,
    ExecutionNotFoundError,
    ICliJobScheduler,
    InvalidOperationError,
    InvalidValueError,
    JobRegistration,
    MAX_JOB_TAG_LENGTH,
    MAX_JOB_TAGS,
    NotSupportedError,
    PRUNE_HISTORY_JOB_ID,
    job_has_tags,
    normalize_job_tags,
)
from .cli_jobs_builder import CliJobsBuilder, CliJobsPlugin
from .cli_jobs_controller import create_cli_jobs_router
from .in_memory_job_storage_provider import InMemoryJobStorageProvider
from .interval_parser import parse_interval
from .prune_history_job import PruneJobHistoryJob

__all__ = [
    "BroadcastFn",
    "CliJobExecutionContext",
    "CliJobLogger",
    "CliJobScheduler",
    "CliJobsBuilder",
    "CliJobsPlugin",
    "ExecutionNotFoundError",
    "ICliJobScheduler",
    "InMemoryJobStorageProvider",
    "InvalidOperationError",
    "InvalidValueError",
    "JobRegistration",
    "MAX_JOB_TAGS",
    "MAX_JOB_TAG_LENGTH",
    "NotSupportedError",
    "PRUNE_HISTORY_JOB_ID",
    "PruneJobHistoryJob",
    "create_cli_jobs_router",
    "job_has_tags",
    "normalize_job_tags",
    "parse_interval",
]
