from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from .log_entry import JobLogEntry


@dataclass
class JobExecution:
    """Record of a single job execution."""

    id: str = ""
    job_id: str = ""
    job_name: str = ""
    status: str = "running"  # 'running' | 'completed' | 'failed' | 'cancelled' | 'timed_out'
    started_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    completed_at: datetime.datetime | None = None
    duration: float | None = None  # milliseconds
    error: str | None = None
    logs: list[JobLogEntry] = field(default_factory=list)
    retry_attempt: int = 0
    #: The host's own values for this execution (e.g. ``items`` and
    #: ``changed`` counts): set by the job (``context.properties``), saved with
    #: the execution and returned by the jobs API.  Values must serialize to JSON.
    properties: dict[str, Any] = field(default_factory=dict)
