from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CliServerCommandParameterDescriptorDto(BaseModel):
    name: str
    description: str
    required: bool = False
    type: str = "string"
    aliases: list[str] | None = None
    default_value: Any = Field(alias="defaultValue", default=None)

    model_config = {"populate_by_name": True}


class CliServerCommandDescriptor(BaseModel):
    command: str
    description: str
    version: str = "1.0.0"
    parameters: list[CliServerCommandParameterDescriptorDto] | None = None
    processors: list["CliServerCommandDescriptor"] | None = None
    allow_unlisted_commands: bool | None = Field(alias="allowUnlistedCommands", default=None)
    value_required: bool | None = Field(alias="valueRequired", default=None)
    author: dict[str, str] | None = None

    model_config = {"populate_by_name": True}
