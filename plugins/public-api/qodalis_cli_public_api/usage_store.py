"""Request counters of the public API, per token and window.

A window is a UTC day (``yyyy-MM-dd``) or a UTC minute (``yyyy-MM-ddTHH:mm``).
Counters are kept per token for display (token usage) and summed per account
(``user_id``) for the limits, so revoked tokens keep counting.  A persistent
store shared by several processes makes the limits hold across them; the
guard admits with increment, check, then compensate (``decrement``) when a
limit is exceeded.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


def is_day(window: str) -> bool:
    return len(window) == 10


class IPublicApiUsageStore(ABC):
    """Where the public API's request counters live."""

    @abstractmethod
    async def increment(self, token_id: str, user_id: str, window: str) -> int:
        """Add one request of ``token_id`` (owned by ``user_id``) to
        ``window``; returns the token's count in it."""

    @abstractmethod
    async def decrement(self, token_id: str, window: str) -> None:
        """Take back one request of ``token_id`` in ``window`` (a refused
        request that was counted)."""

    @abstractmethod
    async def account_count(self, user_id: str, window: str) -> int:
        """Requests of every token of ``user_id`` in ``window``."""

    @abstractmethod
    async def token_days(self, token_ids: list[str], from_day: str, to_day: str) -> dict[str, dict[str, int]]:
        """``{token_id: {day: count}}`` for the day windows from ``from_day``
        to ``to_day`` (inclusive); tokens and days without requests may be
        left out."""

    @abstractmethod
    async def prune(self, before_day: str, before_minute: str) -> None:
        """Drop day windows before ``before_day`` and minute windows before
        ``before_minute``."""


class InMemoryPublicApiUsageStore(IPublicApiUsageStore):
    """The default store: counters in this process only."""

    def __init__(self) -> None:
        #: ``(token_id, window) -> [user_id, count]``
        self._counts: dict[tuple[str, str], list] = {}
        #: ``(user_id, window) -> count``
        self._accounts: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    async def increment(self, token_id: str, user_id: str, window: str) -> int:
        async with self._lock:
            row = self._counts.setdefault((token_id, window), [user_id, 0])
            row[1] += 1
            key = (row[0], window)
            self._accounts[key] = self._accounts.get(key, 0) + 1
            return row[1]

    async def decrement(self, token_id: str, window: str) -> None:
        async with self._lock:
            row = self._counts.get((token_id, window))
            if row is None or row[1] <= 0:
                return
            row[1] -= 1
            key = (row[0], window)
            self._accounts[key] = max(0, self._accounts.get(key, 0) - 1)

    async def account_count(self, user_id: str, window: str) -> int:
        async with self._lock:
            return self._accounts.get((user_id, window), 0)

    async def token_days(self, token_ids: list[str], from_day: str, to_day: str) -> dict[str, dict[str, int]]:
        wanted = set(token_ids)
        result: dict[str, dict[str, int]] = {}
        async with self._lock:
            for (token_id, window), (_, count) in self._counts.items():
                if token_id in wanted and is_day(window) and from_day <= window <= to_day:
                    result.setdefault(token_id, {})[window] = count
        return result

    async def prune(self, before_day: str, before_minute: str) -> None:
        async with self._lock:
            for key in [k for k in self._counts if _expired(k[1], before_day, before_minute)]:
                del self._counts[key]
            for key in [k for k in self._accounts if _expired(k[1], before_day, before_minute)]:
                del self._accounts[key]


def _expired(window: str, before_day: str, before_minute: str) -> bool:
    return window < before_day if is_day(window) else window < before_minute
