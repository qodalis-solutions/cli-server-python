from __future__ import annotations

from ..abstractions import ICliCommandProcessor
from ..services.cli_command_registry import CliCommandRegistry


class CliBuilder:
    def __init__(self, registry: CliCommandRegistry) -> None:
        self._registry = registry

    def add_processor(self, processor: ICliCommandProcessor) -> CliBuilder:
        self._registry.register(processor)
        return self

    @property
    def registry(self) -> CliCommandRegistry:
        return self._registry
