"""The behaviour every config store shares (memory, JSON file, JSON file
section, database, Redis): subclass :class:`ConfigStoreContract` in a
``Test*`` class with a ``store`` fixture.  Used by ``test_config_stores.py``
and the config-redis plugin's tests.

Ported from cli-server-node ``src/__tests__/config-store-contract.ts``."""

from __future__ import annotations

import asyncio

import pytest

from qodalis_cli_server_abstractions.config import CliConfigConflictError, CliConfigStoredSection


def section(values: dict | None = None, version: int = 0) -> CliConfigStoredSection:
    return CliConfigStoredSection(values or {}, version)


class ConfigStoreContract:
    """The store contract; a subclass provides the ``store`` fixture."""

    async def test_unknown_section_is_empty_at_version_0(self, store):
        assert await store.read("p", "s") == section({}, 0)

    async def test_writes_bump_version_by_one_and_keep_values(self, store):
        first = await store.write("p", "s", {"a": 1, "b": ["x"]})
        assert first == section({"a": 1, "b": ["x"]}, 1)
        second = await store.write("p", "s", {"c": {"deep": True}})
        assert second == section({"a": 1, "b": ["x"], "c": {"deep": True}}, 2)
        assert await store.read("p", "s") == second

    async def test_none_removes_a_key(self, store):
        await store.write("p", "s", {"a": 1, "b": 2})
        after = await store.write("p", "s", {"a": None})
        assert after.values == {"b": 2}
        assert after.version == 2

    async def test_sections_and_providers_are_independent(self, store):
        await store.write("p", "s1", {"a": 1})
        await store.write("p", "s2", {"a": 2})
        await store.write("q", "s1", {"a": 3})
        assert (await store.read("p", "s1")).values == {"a": 1}
        assert (await store.read("p", "s2")).values == {"a": 2}
        assert (await store.read("q", "s1")).values == {"a": 3}
        assert (await store.read("q", "s1")).version == 1

    async def test_stale_expected_version_is_conflict_with_current_version(self, store):
        await store.write("p", "s", {"a": 1}, 0)
        await store.write("p", "s", {"a": 2}, 1)
        with pytest.raises(CliConfigConflictError) as info:
            await store.write("p", "s", {"a": 3}, 1)
        assert info.value.current_version == "2"
        assert (await store.read("p", "s")).values == {"a": 2}

    async def test_first_write_with_expected_version_other_than_0_is_conflict(self, store):
        with pytest.raises(CliConfigConflictError):
            await store.write("p", "new", {"a": 1}, 3)
        assert await store.read("p", "new") == section({}, 0)

    async def test_returned_values_are_copies(self, store):
        written = await store.write("p", "s", {"list": ["a"]})
        written.values["list"].append("mutated")
        read = await store.read("p", "s")
        read.values["list"].append("again")
        assert (await store.read("p", "s")).values["list"] == ["a"]

    async def test_concurrent_writes_without_expected_version_never_lose_a_change(self, store):
        await asyncio.gather(*(store.write("p", "s", {f"k{i}": i}) for i in range(5)))
        final = await store.read("p", "s")
        assert final.version == 5
        assert sorted(final.values) == ["k0", "k1", "k2", "k3", "k4"]

    async def test_one_store_keeps_two_providers_apart(self, store):
        await store.write("jobs", "history", {"maxAge": "30d"})
        await store.write("jobs", "history", {"maxRunsPerJob": 10})
        await store.write("users", "history", {"maxAge": "1d"})
        assert await store.read("jobs", "history") == section({"maxAge": "30d", "maxRunsPerJob": 10}, 2)
        assert await store.read("users", "history") == section({"maxAge": "1d"}, 1)
        with pytest.raises(CliConfigConflictError):
            await store.write("users", "history", {"maxAge": "2d"}, 2)
        assert (await store.write("jobs", "history", {"maxAge": "7d"}, 2)).version == 3

    async def test_secrets_are_stored_as_given(self, store):
        await store.write("p", "s", {"apiKey": "sk_live_1"})
        assert (await store.read("p", "s")).values["apiKey"] == "sk_live_1"
