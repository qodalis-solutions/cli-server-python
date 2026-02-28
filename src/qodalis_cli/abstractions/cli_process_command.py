from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CliProcessCommand:
    command: str = ""
    raw_command: str = ""
    value: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    chain_commands: list[str] = field(default_factory=list)
    data: Any = None
