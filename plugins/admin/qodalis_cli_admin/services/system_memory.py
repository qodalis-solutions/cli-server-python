"""The machine's (or container's) memory, as ``GET /status`` reports it:
``systemMemoryTotalMb``, ``systemMemoryUsedMb`` and ``memoryLimited``."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import psutil

#: cgroup v2, then v1: the container's memory limit.
CGROUP_LIMIT_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
#: cgroup v2, then v1: the container's memory usage.
CGROUP_USAGE_FILES = ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes")

Reader = Callable[[], Optional[float]]


def _read_first_number(files: tuple[str, ...]) -> float | None:
    for name in files:
        try:
            text = Path(name).read_text().strip()
        except OSError:
            continue
        if text == "max":  # cgroup v2 without a limit
            return None
        try:
            return float(text)
        except ValueError:
            continue
    return None


def _installed_bytes() -> float | None:
    return float(psutil.virtual_memory().total)


def _available_bytes() -> float | None:
    # On Linux psutil reads MemAvailable of /proc/meminfo; on macOS and Windows the OS's own figure.
    return float(psutil.virtual_memory().available)


@dataclass
class SystemMemoryReaders:
    """Where :func:`read_system_memory` reads from, in bytes; replaceable in
    tests. Any reader may raise or return ``None``."""

    #: Installed RAM.
    total_bytes: Reader = _installed_bytes
    #: Available RAM of the machine.
    available_bytes: Reader = _available_bytes
    #: The container limit (cgroup ``memory.max`` / ``memory.limit_in_bytes``); a huge number without one.
    limit_bytes: Reader = lambda: _read_first_number(CGROUP_LIMIT_FILES)  # noqa: E731
    #: The container's usage (cgroup ``memory.current`` / ``memory.usage_in_bytes``).
    cgroup_usage_bytes: Reader = lambda: _read_first_number(CGROUP_USAGE_FILES)  # noqa: E731


#: The readers of this process.
default_readers = SystemMemoryReaders()


def _safe(read: Reader) -> float | None:
    try:
        value: Any = read()
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _to_mb(n: float) -> int:
    return int(round(n / (1024 * 1024)))


def read_system_memory(readers: SystemMemoryReaders | None = None) -> dict[str, Any]:
    """``systemMemoryTotalMb``: the RAM the server can use (the container limit
    when one is set below the installed RAM, else the installed RAM);
    ``systemMemoryUsedMb``: the container's usage when limited, else total
    minus available; ``memoryLimited``. Each is ``None`` when unknown; never
    raises."""
    r = readers or default_readers
    installed = _safe(r.total_bytes)
    limit = _safe(r.limit_bytes)
    limited = limit is not None and installed is not None and limit < installed
    total = limit if limited else installed
    if total is None:
        return {"systemMemoryTotalMb": None, "systemMemoryUsedMb": None, "memoryLimited": None}

    used: float | None = None
    if limited:
        used = _safe(r.cgroup_usage_bytes)
    else:
        available = _safe(r.available_bytes)
        if available is not None and available <= total:
            used = total - available
    return {
        "systemMemoryTotalMb": _to_mb(total),
        "systemMemoryUsedMb": None if used is None else min(_to_mb(used), _to_mb(total)),
        "memoryLimited": limited,
    }
