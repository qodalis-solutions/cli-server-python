from __future__ import annotations

import abc
from typing import Sequence

from ..abstractions import ICliCommandProcessor, command_matches


class ICliCommandRegistry(abc.ABC):
    """Interface for a registry that stores and looks up command processors."""

    @property
    @abc.abstractmethod
    def processors(self) -> Sequence[ICliCommandProcessor]:
        """All registered top-level processors."""
        ...

    @abc.abstractmethod
    def register(self, processor: ICliCommandProcessor, module: str | None = None) -> None:
        """Register a command processor.

        Args:
            processor: The processor to register.
            module: The name of the module that added it (``add_module``), if any.
        """
        ...

    def module_of(self, processor: ICliCommandProcessor) -> str | None:
        """The name of the module that registered ``processor``, if any. ``GET /commands``
        lists it as the command's ``module`` when the processor names none itself."""
        return None

    @abc.abstractmethod
    def find_processor(
        self,
        command: str,
        chain_commands: list[str] | None = None,
    ) -> ICliCommandProcessor | None:
        """Find a processor by command name, optionally traversing sub-commands.

        Args:
            command: The top-level command name.
            chain_commands: Optional list of sub-command names to traverse.

        Returns:
            The matching processor, or ``None`` if not found.
        """
        ...


class CliCommandRegistry(ICliCommandRegistry):
    """Default in-memory command registry keyed by lowercase command name."""

    def __init__(self) -> None:
        self._processors: dict[str, ICliCommandProcessor] = {}
        self._modules: dict[str, str] = {}

    @property
    def processors(self) -> Sequence[ICliCommandProcessor]:
        return list(self._processors.values())

    def register(self, processor: ICliCommandProcessor, module: str | None = None) -> None:
        key = processor.command.lower()
        self._processors[key] = processor
        if module and module.strip():
            self._modules[key] = module
        else:
            self._modules.pop(key, None)

    def module_of(self, processor: ICliCommandProcessor) -> str | None:
        key = processor.command.lower()
        return self._modules.get(key) if self._processors.get(key) is processor else None

    def find_processor(
        self,
        command: str,
        chain_commands: list[str] | None = None,
    ) -> ICliCommandProcessor | None:
        # A command name wins over another command's alias
        processor = self._processors.get(command.lower())
        if processor is None:
            processor = next((p for p in self._processors.values() if command_matches(p, command)), None)
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
        """Walk the sub-processor tree following the chain of command names."""
        current = processor
        for sub in chain:
            subs = current.processors
            if not subs:
                if current.allow_unlisted_commands:
                    return current
                return None
            found = next((p for p in subs if p.command.lower() == sub.lower()), None) or next(
                (p for p in subs if command_matches(p, sub)), None,
            )
            if found is None:
                if current.allow_unlisted_commands:
                    return current
                return None
            current = found
        return current
