from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ICliCommandAuthor(Protocol):
    """Protocol describing a command author with name and email."""

    name: str
    email: str


class CliCommandAuthor:
    """Concrete implementation of a CLI command author."""

    def __init__(self, name: str, email: str) -> None:
        self.name = name
        self.email = email


DEFAULT_LIBRARY_AUTHOR = CliCommandAuthor("Nicolae Lupei", "nicolae.lupei@qodalis.com")
"""Default author used when a processor does not specify one."""
