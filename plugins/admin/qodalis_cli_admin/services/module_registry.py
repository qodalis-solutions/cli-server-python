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
            module_id = self._module_id(idx, module)
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
        return [
            self._describe(idx, module, self._module_id(idx, module) not in self._disabled)
            for idx, module in enumerate(self._builder.modules)
        ]

    def toggle(self, module_id: str) -> dict[str, Any]:
        """Toggle a module's enabled state and return the updated record.

        ``module_id`` is the module name (as listed); the legacy numeric index
        is still accepted.
        """
        modules = self._builder.modules
        idx = self._find(module_id)
        if idx is None:
            raise KeyError(f"Module not found: {module_id}")

        module_key = self._module_id(idx, modules[idx])
        if module_key in self._disabled:
            self._disabled.discard(module_key)
            enabled = True
        else:
            self._disabled.add(module_key)
            enabled = False
        return self._describe(idx, modules[idx], enabled)

    # -- helpers ------------------------------------------------------------

    def _module_id(self, idx: int, module: Any) -> str:
        """Stable id: the module name (same as the node and .NET servers);
        the index only when the name is missing or used twice."""
        name = getattr(module, "name", None)
        names = [getattr(m, "name", None) for m in self._builder.modules]
        if isinstance(name, str) and name and names.count(name) == 1:
            return name
        return str(idx)

    def _find(self, module_id: str) -> int | None:
        modules = self._builder.modules
        for idx, module in enumerate(modules):
            if self._module_id(idx, module) == module_id:
                return idx
        if module_id.isdigit() and int(module_id) < len(modules):
            return int(module_id)
        return None

    def _describe(self, idx: int, module: Any, enabled: bool) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self._module_id(idx, module),
            "name": getattr(module, "name", type(module).__name__),
            "version": getattr(module, "version", None) or "",
            "description": getattr(module, "description", None) or "",
            "enabled": enabled,
            "processorCount": len(module.processors),
            "processors": [p.command for p in module.processors],
        }
        author = getattr(module, "author", None)
        if author is not None:
            record["author"] = getattr(author, "name", author) if not isinstance(author, str) else author
        return record
