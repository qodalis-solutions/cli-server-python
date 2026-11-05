"""Status history: samples the values of ``GET /status`` every 15 seconds and
keeps the last 24 hours in memory (``GET /status/history``)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

import psutil

logger = logging.getLogger(__name__)

#: Seconds between two samples.
SAMPLE_INTERVAL_SECONDS = 15
#: 24 hours of samples.
MAX_SAMPLES = 24 * 60 * 60 // SAMPLE_INTERVAL_SECONDS


def memory_usage_mb() -> float:
    """The process's resident memory in MB, as ``GET /status`` reports it."""
    rss = psutil.Process(os.getpid()).memory_info().rss
    return round(rss / (1024 * 1024), 2)


class StatusHistory:
    """Ring buffer of status samples (oldest first) and the asyncio task
    that fills it.  The task runs while the app runs (see :meth:`lifespan`)
    and is cancelled on shutdown, so it never keeps the process alive."""

    def __init__(
        self,
        event_socket_manager: Any = None,
        *,
        interval: float = SAMPLE_INTERVAL_SECONDS,
        max_samples: int = MAX_SAMPLES,
        clock: Callable[[], float] = time.monotonic,
        cpu_clock: Callable[[], float] = time.process_time,
        memory: Callable[[], float] = memory_usage_mb,
    ) -> None:
        self._event_socket_manager = event_socket_manager
        self.interval = interval
        self.samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._clock = clock
        self._cpu_clock = cpu_clock
        self._memory = memory
        self._last: tuple[float, float] | None = None
        self._task: asyncio.Task[None] | None = None

    def sample(self, now: datetime | None = None) -> dict[str, Any]:
        """Take one sample and append it."""
        wall, cpu = self._clock(), self._read_cpu()
        cpu_percent: float | None = None
        if self._last is not None and cpu is not None and self._last[1] is not None:
            elapsed = wall - self._last[0]
            if elapsed > 0:
                cpu_percent = round(max(cpu - self._last[1], 0.0) / elapsed * 100, 2)
        self._last = (wall, cpu)
        sample = {
            "timestamp": (now or datetime.now(timezone.utc)),
            "memoryMb": self._memory(),
            "cpuPercent": cpu_percent,
            "activeWsConnections": self._ws_connections(),
            "activeShellSessions": 0,
        }
        self.samples.append(sample)
        return sample

    def history(self, minutes: int, now: datetime | None = None) -> list[dict[str, Any]]:
        """The samples of the last ``minutes``, oldest first, as JSON."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=minutes)
        return [
            {**s, "timestamp": _iso_ms(s["timestamp"])}
            for s in list(self.samples)
            if s["timestamp"] >= cutoff
        ]

    def start(self) -> None:
        """Start sampling (first sample now) on the running event loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Cancel the sampler and wait for it to end."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def wrap_lifespan(self, lifespan: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """The app's lifespan with the sampler running inside it."""

        @asynccontextmanager
        async def wrapped(app: Any) -> AsyncIterator[Any]:
            async with lifespan(app) as state:
                self.start()
                try:
                    yield state
                finally:
                    await self.stop()

        return wrapped

    async def _run(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:  # never let one failed sample stop the history
                logger.debug("Status sample failed", exc_info=True)
            await asyncio.sleep(self.interval)

    def _read_cpu(self) -> float | None:
        try:
            return self._cpu_clock()
        except Exception:
            return None

    def _ws_connections(self) -> int:
        manager = self._event_socket_manager
        if manager is None:
            return 0
        try:
            return len(manager.get_clients())
        except Exception:
            return 0


def _iso_ms(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
