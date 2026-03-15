from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass
class JobLogEntry:
    """A single log entry captured during job execution."""

    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    level: str = "info"  # 'debug' | 'info' | 'warning' | 'error'
    message: str = ""
