from __future__ import annotations

import abc


class ICliJobLogger(abc.ABC):
    """Logger interface provided to jobs during execution."""

    @abc.abstractmethod
    def debug(self, message: str) -> None:
        """Log a debug-level message."""
        ...

    @abc.abstractmethod
    def info(self, message: str) -> None:
        """Log an info-level message."""
        ...

    @abc.abstractmethod
    def warning(self, message: str) -> None:
        """Log a warning-level message."""
        ...

    @abc.abstractmethod
    def error(self, message: str) -> None:
        """Log an error-level message."""
        ...
