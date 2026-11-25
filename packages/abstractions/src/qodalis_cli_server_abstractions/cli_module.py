from __future__ import annotations

import abc
from typing import Any, Sequence

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

    @property
    def permissions(self) -> Sequence[Any]:
        """Permissions its commands require (``required_permissions``), added
        to the server's permission catalog with the module (``add_module``,
        surface ``cli``): ``{"name", "description", "group"}`` dicts or
        ``CliPermission`` entries; a name already in the catalog stays as
        registered."""
        return []

    @property
    def config_providers(self) -> Sequence[Any]:
        """Config providers the module brings (``ICliConfigProvider``: its
        settings), registered with the server's when the module is added."""
        return []

    @property
    def icon(self) -> str | None:
        """One emoji or an image URL shown for the module (the admin
        dashboard's plugins page); ``None`` = none."""
        return None


class CliModule(ICliModule):
    """Base class for CLI modules providing sensible defaults."""
    pass
