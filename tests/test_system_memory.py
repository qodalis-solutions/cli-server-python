"""The machine's (or container's) memory of the admin status: installed RAM and available memory, a container
limit below the installed RAM with the cgroup's usage, None for what cannot be read, and the ``admin status``
Memory line."""

from __future__ import annotations

from qodalis_cli_admin import SystemMemoryReaders, format_memory_line, format_memory_size, read_system_memory
from qodalis_cli_admin.services.status_history import StatusHistory

MB = 1024 * 1024
GB = 1024 * MB


def readers(**overrides) -> SystemMemoryReaders:  # noqa: ANN003
    values = {
        "total_bytes": lambda: 16 * GB,
        "available_bytes": lambda: 6 * GB,
        "limit_bytes": lambda: None,
        "cgroup_usage_bytes": lambda: None,
    }
    values.update(overrides)
    return SystemMemoryReaders(**values)


def boom() -> float:
    raise OSError("unreadable")


def test_without_a_limit_the_installed_ram_and_the_machine_use():
    assert read_system_memory(readers()) == {"systemMemoryTotalMb": 16384, "systemMemoryUsedMb": 10240, "memoryLimited": False}


def test_a_container_limit_below_the_installed_ram_and_its_cgroup_usage():
    memory = read_system_memory(readers(limit_bytes=lambda: 512 * MB, cgroup_usage_bytes=lambda: 300 * MB))
    assert memory == {"systemMemoryTotalMb": 512, "systemMemoryUsedMb": 300, "memoryLimited": True}


def test_a_limit_at_or_above_the_installed_ram_is_not_a_limit():
    assert read_system_memory(readers(limit_bytes=lambda: 16 * GB))["memoryLimited"] is False
    # cgroup v1 without a limit: a huge number
    assert read_system_memory(readers(limit_bytes=lambda: 9223372036854771712))["systemMemoryTotalMb"] == 16384


def test_limited_without_a_readable_cgroup_usage_used_is_none():
    memory = read_system_memory(readers(limit_bytes=lambda: 512 * MB))
    assert memory == {"systemMemoryTotalMb": 512, "systemMemoryUsedMb": None, "memoryLimited": True}


def test_none_for_what_cannot_be_read_and_never_raises():
    assert read_system_memory(SystemMemoryReaders(boom, boom, boom, boom)) == {
        "systemMemoryTotalMb": None, "systemMemoryUsedMb": None, "memoryLimited": None,
    }
    assert read_system_memory(readers(available_bytes=boom)) == {
        "systemMemoryTotalMb": 16384, "systemMemoryUsedMb": None, "memoryLimited": False,
    }
    assert read_system_memory(readers(total_bytes=lambda: float("nan")))["systemMemoryTotalMb"] is None


def test_reads_this_machine_with_the_default_readers():
    memory = read_system_memory()
    assert memory["systemMemoryTotalMb"] > 0
    assert isinstance(memory["memoryLimited"], bool)


def test_every_sample_carries_the_machine_memory():
    system = {"systemMemoryTotalMb": 512, "systemMemoryUsedMb": 300, "memoryLimited": True}
    sample = StatusHistory(None, memory=lambda: 1.0, system_memory=lambda: system).sample()
    assert sample["systemMemoryUsedMb"] == 300 and sample["systemMemoryTotalMb"] == 512
    sample = StatusHistory(None, memory=lambda: 1.0, system_memory=boom).sample()
    assert sample["systemMemoryUsedMb"] is None and sample["systemMemoryTotalMb"] is None


def _line(total, used, limited) -> str:  # noqa: ANN001
    return format_memory_line({"memoryUsageMb": 146, "systemMemoryTotalMb": total, "systemMemoryUsedMb": used, "memoryLimited": limited})


def test_memory_line_the_process_then_the_machine_or_container_use():
    assert _line(16384, 10035, False) == "146 MB (process) · machine 9.8 GB of 16 GB used (61%)"
    assert _line(512, 300, True) == "146 MB (process) · container 300 MB of 512 MB used (59%)"


def test_memory_line_the_total_alone_without_the_use_the_process_alone_without_the_total():
    assert _line(16384, None, False) == "146 MB (process) · machine 16 GB"
    assert _line(512, None, True) == "146 MB (process) · container limit 512 MB"
    assert _line(None, None, None) == "146 MB"


def test_memory_sizes():
    assert format_memory_size(1023) == "1023 MB"
    assert format_memory_size(1024) == "1 GB"
    assert format_memory_size(1536) == "1.5 GB"
