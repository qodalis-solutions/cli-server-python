"""Module registry — reads modules from CliBuilder and tracks enabled state.

Implements :class:`ICliProcessorFilter` so that processors belonging to
disabled modules are blocked at execution time.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from qodalis_cli_server_abstractions import ICliProcessorFilter

if TYPE_CHECKING:
    from qodalis_cli_server_abstractions import ICliCommandProcessor
    from qodalis_cli.extensions.cli_builder import CliBuilder


class ModuleRegistry(ICliProcessorFilter):
    """Provides a read/toggle view over modules registered in :class:`CliBuilder`.

    Also acts as a processor filter: processors that belong to a disabled
    module are reported as *not allowed*, causing the executor to reject
    the command with a user-friendly error.
    """

    def __init__(self, builder: CliBuilder) -> None:
        self._builder = builder
        self._disabled: set[str] = set()
        # Map each processor instance to its module id for fast lookup.
        self._processor_to_module: dict[int, str] = {}
        self._rebuild_processor_map()

    def _rebuild_processor_map(self) -> None:
        """(Re-)build the processor-to-module-id mapping."""
        self._processor_to_module.clear()
        for idx, module in enumerate(self._builder.modules):
            module_id = str(idx)
            for processor in module.processors:
                self._processor_to_module[id(processor)] = module_id

    # -- ICliProcessorFilter ------------------------------------------------

    def is_allowed(self, processor: ICliCommandProcessor) -> bool:
        """Return ``False`` if the processor belongs to a disabled module."""
        module_id = self._processor_to_module.get(id(processor))
        if module_id is None:
            # Processor not registered via a module — always allowed.
            return True
        return module_id not in self._disabled

    # -- Public API ---------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """Return a list of all modules with their enabled/disabled state."""
        result: list[dict[str, Any]] = []
        for idx, module in enumerate(self._builder.modules):
            module_id = str(idx)
            name = getattr(module, "name", type(module).__name__)
            result.append(
                {
                    "id": module_id,
                    "name": name,
                    "processorCount": len(module.processors),
                    "processors": [p.command for p in module.processors],
                    "enabled": module_id not in self._disabled,
                }
            )
        return result

    def toggle(self, module_id: str) -> dict[str, Any]:
        """Toggle a module's enabled state and return the updated record."""
        modules = self._builder.modules
        idx = int(module_id)
        if idx < 0 or idx >= len(modules):
            raise KeyError(f"Module not found: {module_id}")

        if module_id in self._disabled:
            self._disabled.discard(module_id)
            enabled = True
        else:
            self._disabled.add(module_id)
            enabled = False

        module = modules[idx]
        result: dict[str, Any] = {
            "id": module_id,
            "name": getattr(module, "name", type(module).__name__),
            "processorCount": len(module.processors),
            "processors": [p.command for p in module.processors],
            "enabled": enabled,
        }

        return result
