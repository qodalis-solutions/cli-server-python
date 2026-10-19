from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CliProcessCommand:
    """Parsed representation of a CLI command submitted for execution."""

    command: str = ""
    raw_command: str = ""
    value: str | None = None
    """The positional text as typed (quotes included)."""
    args: dict[str, Any] = field(default_factory=dict)
    chain_commands: list[str] = field(default_factory=list)
    data: Any = None
    positionals: list[str] | None = None
    """Positional arguments split like a shell (quotes removed, flags excluded);
    ``None`` when the client did not send them.  Use ``get_positionals()``."""
