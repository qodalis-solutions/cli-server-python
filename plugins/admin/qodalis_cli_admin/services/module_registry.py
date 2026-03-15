"""Module registry — reads modules from CliBuilder and tracks enabled state."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from qodalis_cli.extensions.cli_builder import CliBuilder


class ModuleRegistry:
    """Provides a read/toggle view over modules registered in :class:`CliBuilder`."""

    def __init__(self, builder: CliBuilder) -> None:
        self._builder = builder
        self._disabled: set[str] = set()

    def list(self) -> list[dict[str, Any]]:
        """Return a list of all modules with their enabled/disabled state."""
        result: list[dict[str, Any]] = []
        for idx, module in enumerate(self._builder.modules):
            module_id = str(idx)
            name = type(module).__name__
            result.append(
                {
                    "id": module_id,
                    "name": name,
                    "processorCount": len(module.processors),
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
        else:
            self._disabled.add(module_id)

        module = modules[idx]
        return {
            "id": module_id,
            "name": type(module).__name__,
            "processorCount": len(module.processors),
            "enabled": module_id not in self._disabled,
        }
