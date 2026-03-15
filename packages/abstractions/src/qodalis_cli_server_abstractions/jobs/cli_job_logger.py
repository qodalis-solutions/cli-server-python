from __future__ import annotations

import abc


class ICliJobLogger(abc.ABC):
    """Logger interface provided to jobs during execution."""

    @abc.abstractmethod
    def debug(self, message: str) -> None: ...

    @abc.abstractmethod
    def info(self, message: str) -> None: ...

    @abc.abstractmethod
    def warning(self, message: str) -> None: ...

    @abc.abstractmethod
    def error(self, message: str) -> None: ...
