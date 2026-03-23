from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ICliCommandParameterDescriptor(Protocol):
    """Protocol describing a parameter accepted by a CLI command."""

    name: str
    description: str
    required: bool
    type: str
    aliases: list[str] | None
    default_value: Any


class CliCommandParameterDescriptor:
    """Concrete implementation of a CLI command parameter descriptor."""

    def __init__(
        self,
        name: str,
        description: str,
        required: bool = False,
        type: str = "string",
        aliases: list[str] | None = None,
        default_value: Any = None,
    ) -> None:
        self.name = name
        self.description = description
        self.required = required
        self.type = type
        self.aliases = aliases
        self.default_value = default_value
