"""Module registry — reads modules from CliBuilder and tracks enabled state.

Implements :class:`ICliProcessorFilter` so that processors belonging to
disabled modules are blocked at execution time.

Commands registered on their own (``builder.add_processor(...)``) join the
module their ``module`` attribute names (ignoring case): a module added with
that name (it may have no processors of its own and only describe the group:
description, version, icon), or else a module of that name made from the
commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Callable, Iterable, TYPE_CHECKING

from qodalis_cli_server_abstractions import ICliProcessorFilter

if TYPE_CHECKING:
    from qodalis_cli_server_abstractions import ICliCommandProcessor
    from qodalis_cli.extensions.cli_builder import CliBuilder


@dataclass
class _Entry:
    """A listed module: one added with ``add_module``, or a group of commands
    added on their own (``module`` is ``None``)."""

    id: str
    name: str
    module: Any = None
    processors: list[Any] = field(default_factory=list)
    version: str = ""
    description: str = ""
    author: str | None = None


def _standalone_processors(builder: Any) -> Callable[[], Iterable[Any]]:
    """Every processor registered on ``builder`` (``CliBuilder.processors``,
    or its command registry on older servers); none for a stub builder."""

    def read() -> Iterable[Any]:
        processors = getattr(builder, "processors", None)
        if processors is None:
            registry = getattr(builder, "_registry", None)
            processors = getattr(registry, "processors", None)
        if callable(processors):
            processors = processors()
        return list(processors or [])

    return read


def _package_info(processor: Any) -> tuple[str, str, str | None]:
    """Version, summary and author of the distribution shipping
    ``processor`` (empty for the host application's own code)."""
    top = (type(processor).__module__ or "").split(".")[0]
    if not top or top == "__main__":
        return "", "", None
    try:
        dists = metadata.packages_distributions().get(top) or []
        if not dists:
            return "", "", None
        meta = metadata.metadata(dists[0])
        return meta.get("Version") or "", meta.get("Summary") or "", meta.get("Author") or None
    except Exception:  # pragma: no cover - broken metadata
        return "", "", None


class ModuleRegistry(ICliProcessorFilter):
    """Provides a read/toggle view over modules registered in :class:`CliBuilder`.

    Also acts as a processor filter: processors that belong to a disabled
    module are reported as *not allowed*, causing the executor to reject
    the command with a user-friendly error.
    """

    def __init__(
        self,
        builder: CliBuilder,
        processors: Callable[[], Iterable[ICliCommandProcessor]] | None = None,
    ) -> None:
        """``processors`` returns every registered command processor (read
        when the modules are listed), so commands added on their own are
        grouped by their ``module``; default: the builder's processors."""
        self._builder = builder
        self._processors = processors or _standalone_processors(builder)
        self._disabled: set[str] = set()
        self._entries: list[_Entry] = []
        self._key: tuple[int, ...] | None = None
        # Map each processor instance to its module id for fast lookup.
        self._processor_to_module: dict[int, str] = {}

    # -- entries -------------------------------------------------------------

    def _current(self) -> list[_Entry]:
        """The listed modules, rebuilt when modules or processors were added."""
        modules = self._builder.modules
        standalone = list(self._processors())
        key = (len(modules), *(id(m) for m in modules), *(id(p) for p in standalone))
        if key != self._key:
            self._entries = self._build(modules, standalone)
            self._processor_to_module = {}
            for entry in self._entries:
                for processor in entry.processors:
                    self._processor_to_module.setdefault(id(processor), entry.id)
            self._key = key
        return self._entries

    @staticmethod
    def _build(modules: list[Any], standalone: list[Any]) -> list[_Entry]:
        names = [getattr(m, "name", None) for m in modules]
        entries = []
        for idx, module in enumerate(modules):
            name = getattr(module, "name", None)
            # Stable id: the module name (same as the node and .NET servers);
            # the index only when the name is missing or used twice.
            module_id = name if isinstance(name, str) and name and names.count(name) == 1 else str(idx)
            author = getattr(module, "author", None)
            entries.append(_Entry(
                id=module_id,
                name=name if isinstance(name, str) else type(module).__name__,
                module=module,
                processors=list(module.processors),
                version=getattr(module, "version", None) or "",
                description=getattr(module, "description", None) or "",
                author=None if author is None else (author if isinstance(author, str) else getattr(author, "name", author)),
            ))

        owned = {id(p) for e in entries for p in e.processors}
        groups: dict[str, list[Any]] = {}
        for processor in standalone:
            group = getattr(processor, "module", None)
            if id(processor) in owned or not isinstance(group, str) or not group.strip():
                continue
            # Keyed ignoring case; the first spelling names the group
            key = next((k for k in groups if k.lower() == group.lower()), group)
            groups.setdefault(key, []).append(processor)

        for group, processors in groups.items():
            module = next((e for e in entries if e.name.lower() == group.lower()), None)
            if module is not None:
                module.processors.extend(processors)
                continue
            version, description, author = _package_info(processors[0])
            entries.append(_Entry(
                id=group, name=group, processors=processors,
                version=version, description=description, author=author,
            ))
        return entries

    # -- ICliProcessorFilter ------------------------------------------------

    def is_allowed(self, processor: ICliCommandProcessor) -> bool:
        """Return ``False`` if the processor belongs to a disabled module."""
        self._current()
        module_id = self._processor_to_module.get(id(processor))
        if module_id is None:
            # Processor in no module — always allowed.
            return True
        return module_id not in self._disabled

    # -- Public API ---------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """Return a list of all modules with their enabled/disabled state."""
        return [self._describe(e) for e in self._current()]

    def get(self, module_id: str) -> dict[str, Any]:
        """The module ``module_id`` (name, ignoring case, or legacy index);
        ``KeyError`` when there is none."""
        entry = self._find(module_id)
        if entry is None:
            raise KeyError(f"Module not found: {module_id}")
        return self._describe(entry)

    def toggle(self, module_id: str) -> dict[str, Any]:
        """Toggle a module's enabled state and return the updated record.

        ``module_id`` is the module name (as listed, ignoring case); the
        legacy numeric index is still accepted.
        """
        entry = self._find(module_id)
        if entry is None:
            raise KeyError(f"Module not found: {module_id}")
        if entry.id in self._disabled:
            self._disabled.discard(entry.id)
        else:
            self._disabled.add(entry.id)
        return self._describe(entry)

    # -- helpers ------------------------------------------------------------

    def _find(self, module_id: str) -> _Entry | None:
        entries = self._current()
        for entry in entries:
            if entry.id == module_id:
                return entry
        lower = module_id.lower()
        for entry in entries:
            if entry.id.lower() == lower:
                return entry
        if module_id.isdigit() and int(module_id) < len(self._builder.modules):
            return entries[int(module_id)]
        return None

    def _describe(self, entry: _Entry) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": entry.id,
            "name": entry.name,
            "version": entry.version,
            "description": entry.description,
            # Older abstractions have no ``icon``: None
            "icon": getattr(entry.module, "icon", None) if entry.module is not None else None,
            "enabled": entry.id not in self._disabled,
            "processorCount": len(entry.processors),
            "processors": [p.command for p in entry.processors],
        }
        if entry.author is not None:
            record["author"] = entry.author
        return record
