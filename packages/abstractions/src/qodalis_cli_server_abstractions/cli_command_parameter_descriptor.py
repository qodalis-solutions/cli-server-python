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
    #: The only values the parameter takes (help, completion and a check in
    #: the web CLI, ``enum`` in MCP); optional on custom descriptors.
    allowed_values: list[str] | None


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
        allowed_values: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.required = required
        self.type = type
        self.aliases = aliases
        self.default_value = default_value
        self.allowed_values = allowed_values
