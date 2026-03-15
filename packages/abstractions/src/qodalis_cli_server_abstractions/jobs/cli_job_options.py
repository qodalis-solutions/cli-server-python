from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CliJobOptions:
    """Options controlling how a job is scheduled and executed."""

    name: str | None = None
    description: str | None = None
    group: str | None = None
    schedule: str | None = None  # cron expression
    interval: str | None = None  # e.g. "30s", "5m", "1h", "1d"
    enabled: bool = True
    max_retries: int = 0
    timeout: str | None = None  # e.g. "5m"
    overlap_policy: str = "skip"  # 'skip' | 'queue' | 'cancel'
