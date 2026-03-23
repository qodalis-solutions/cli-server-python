from __future__ import annotations

from pydantic import BaseModel, Field

from .cli_server_output import CliServerOutput


class CliServerResponse(BaseModel):
    """Standardised response envelope returned by all CLI command endpoints."""

    exit_code: int = Field(alias="exitCode", default=0)
    outputs: list[CliServerOutput] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
