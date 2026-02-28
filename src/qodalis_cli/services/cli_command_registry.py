from __future__ import annotations

import abc
from typing import Sequence

from ..abstractions import ICliCommandProcessor


class ICliCommandRegistry(abc.ABC):
    @property
    @abc.abstractmethod
    def processors(self) -> Sequence[ICliCommandProcessor]: ...

    @abc.abstractmethod
    def register(self, processor: ICliCommandProcessor) -> None: ...

    @abc.abstractmethod
    def find_processor(
        self,
        command: str,
        chain_commands: list[str] | None = None,
    ) -> ICliCommandProcessor | None: ...


class CliCommandRegistry(ICliCommandRegistry):
    def __init__(self) -> None:
        self._processors: dict[str, ICliCommandProcessor] = {}

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        return list(self._processors.values())

    def register(self, processor: ICliCommandProcessor) -> None:
        self._processors[processor.command.lower()] = processor

    def find_processor(
        self,
        command: str,
        chain_commands: list[str] | None = None,
    ) -> ICliCommandProcessor | None:
        processor = self._processors.get(command.lower())
        if processor is None:
            return None

        if chain_commands:
            return self._resolve_chain(processor, chain_commands)

        return processor

    def _resolve_chain(
        self,
        processor: ICliCommandProcessor,
        chain: list[str],
    ) -> ICliCommandProcessor | None:
        current = processor
        for sub in chain:
            subs = current.processors
            if not subs:
                if current.allow_unlisted_commands:
                    return current
                return None
            found = next((p for p in subs if p.command.lower() == sub.lower()), None)
            if found is None:
                if current.allow_unlisted_commands:
                    return current
                return None
            current = found
        return current
