"""Registry for Data Explorer providers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .data_explorer_policy import can_export, can_use_source, is_read_only

from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    IDataExplorerProvider,
)


class _RegisteredProvider:
    """Internal wrapper that pairs a provider with its options."""

    __slots__ = ("provider", "options")

    def __init__(
        self,
        provider: IDataExplorerProvider,
        options: DataExplorerProviderOptions,
    ) -> None:
        self.provider = provider
        self.options = options


class DataExplorerRegistry:
    """Stores :class:`IDataExplorerProvider` instances keyed by source name."""

    def __init__(self) -> None:
        self._providers: dict[str, _RegisteredProvider] = {}

    def register(
        self,
        provider: IDataExplorerProvider,
        options: DataExplorerProviderOptions,
    ) -> None:
        """Register a provider under the source name defined in *options*."""
        self._providers[options.name] = _RegisteredProvider(provider, options)

    def get(self, source: str) -> tuple[IDataExplorerProvider, DataExplorerProviderOptions] | None:
        """Return the provider and its options for *source*, or ``None``."""
        entry = self._providers.get(source)
        if entry is None:
            return None
        return entry.provider, entry.options

    def get_sources(self, caller: Any = None) -> list[dict[str, Any]]:
        """Summary information of the sources ``caller`` may use (every source
        without a caller), with ``read_only`` and ``can_export``.  The
        source's policy (permissions, objects, masking) is not listed."""
        sources: list[dict[str, Any]] = []
        for entry in self._providers.values():
            options = entry.options
            if not can_use_source(options, caller):
                continue
            info = {k: v for k, v in asdict(options).items() if k not in _POLICY_FIELDS}
            info["read_only"] = is_read_only(options)
            info["can_export"] = can_export(options, caller)
            sources.append(info)
        return sources


#: Source options that stay on the server (not listed by ``get_sources``).
_POLICY_FIELDS = frozenset({
    "required_permissions",
    "read_only",
    "allowed_objects",
    "mask_columns",
    "drop_columns",
    "audit",
    "rate_limit",
    "export_permissions",
})
