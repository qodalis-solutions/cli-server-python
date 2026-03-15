from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass
class JobState:
    """Persisted state of a job (survives restarts)."""

    status: str = "active"  # 'active' | 'paused' | 'stopped'
    last_run_at: datetime.datetime | None = None
    updated_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
