from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ICliCommandAuthor(Protocol):
    name: str
    email: str


class CliCommandAuthor:
    def __init__(self, name: str, email: str) -> None:
        self.name = name
        self.email = email


DEFAULT_LIBRARY_AUTHOR = CliCommandAuthor("Nicolae Lupei", "nicolae.lupei@qodalis.com")
