from __future__ import annotations

import abc
from typing import Sequence

from .cli_command_processor import ICliCommandProcessor
from .cli_command_author import ICliCommandAuthor, DEFAULT_LIBRARY_AUTHOR


class ICliModule(abc.ABC):
    """Represents a module that bundles one or more command processors."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique name of the module."""
        ...

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Module version."""
        ...

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Short description of the module."""
        ...

    @property
    def author(self) -> ICliCommandAuthor:
        """Author of the module."""
        return DEFAULT_LIBRARY_AUTHOR

    @property
    @abc.abstractmethod
    def processors(self) -> Sequence[ICliCommandProcessor]:
        """Command processors provided by this module."""
        ...


class CliModule(ICliModule):
    """Base class for CLI modules providing sensible defaults."""
    pass
