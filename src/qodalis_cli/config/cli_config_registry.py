"""The config providers of a server and the store of their writable settings."""

from __future__ import annotations

import logging
from typing import Any, Optional

from qodalis_cli_server_abstractions.config import (
    CLI_CONFIG_PROVIDER_ID,
    ICliConfigStore,
    InMemoryConfigStore,
)

logger = logging.getLogger("qodalis_cli.config")

#: Default ``order`` of a provider that sets none.
DEFAULT_CONFIG_PROVIDER_ORDER = 100


def _order(provider: Any) -> int:
    order = getattr(provider, "order", None)
    return order if isinstance(order, int) and not isinstance(order, bool) else DEFAULT_CONFIG_PROVIDER_ORDER


class CliConfigRegistry:
    """The config providers of a server (``builder.config_providers``) and the
    store their writable settings are kept in (``builder.set_config_store``,
    in memory by default).  Started by ``create_cli_server``: from then on the
    store is attached to every provider that keeps its settings in it
    (``attach_store``, e.g. a ``CliStoredConfigProvider``) and the stored
    values are applied, at the app's startup or on the first config request,
    whichever comes first."""

    def __init__(self) -> None:
        self._providers: list[Any] = []
        self._store: ICliConfigStore = InMemoryConfigStore()
        self._started = False
        self._pending: list[Any] = []
        self._error: Optional[BaseException] = None

    def add(self, provider: Any) -> None:
        """Registers a provider.  Raises ``ValueError``: ``invalid config
        provider: <id>`` or ``config provider already registered: <id>``."""
        provider_id = getattr(provider, "id", None)
        if (
            not isinstance(provider_id, str)
            or not CLI_CONFIG_PROVIDER_ID.match(provider_id)
            or not callable(getattr(provider, "get_sections", None))
        ):
            raise ValueError(f"invalid config provider: {provider_id}")
        title = getattr(provider, "title", None)
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"invalid config provider: {provider_id} needs a title")
        if self.has(provider_id):
            raise ValueError(f"config provider already registered: {provider_id}")
        self._providers.append(provider)
        if self._started and callable(getattr(provider, "attach_store", None)):
            self._pending.append(provider)

    def has(self, provider_id: str) -> bool:
        """True when a provider with this id is registered."""
        return any(p.id == provider_id for p in self._providers)

    def find(self, provider_id: str) -> Any:
        return next((p for p in self._providers if p.id == provider_id), None)

    @property
    def providers(self) -> list[Any]:
        """The providers sorted by ``order`` (default 100), then id."""
        return sorted(self._providers, key=lambda p: (_order(p), p.id))

    @property
    def store(self) -> ICliConfigStore:
        """The store of writable settings."""
        return self._store

    def set_store(self, store: ICliConfigStore) -> None:
        """Keeps writable settings in ``store``; only before the server starts the registry."""
        if self._started:
            raise RuntimeError("the config store must be set before the server is created")
        self._store = store

    def start(self) -> None:
        """Attaches the store to the providers (once, see :meth:`ready`);
        providers added later are attached as well."""
        if self._started:
            return
        self._started = True
        self._pending.extend(p for p in self._providers if callable(getattr(p, "attach_store", None)))

    @property
    def started(self) -> bool:
        return self._started

    async def ready(self) -> None:
        """Attaches the store to the providers not attached yet, which load and
        apply their stored settings.  Raises the first failure (again on every
        call: a config store that cannot be read fails every config request)."""
        while self._pending:
            provider = self._pending.pop(0)
            try:
                await provider.attach_store(self._store)
            except Exception as exc:
                logger.error("Loading the settings of config provider '%s' failed: %s", provider.id, exc)
                if self._error is None:
                    self._error = exc
        if self._error is not None:
            raise self._error
