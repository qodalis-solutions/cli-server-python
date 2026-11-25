"""CliRedisConfigStore (qodalis-cli-server-plugin-config-redis): the store
contract on a fake Redis (fakeredis when installed with Lua support, else an
in-test fake running the write script's logic), keys and hashes, the atomic
version check under concurrent writers, TTL, describe() without
credentials, and per-provider use in a server.  With QCLI_TEST_REDIS (a
redis:// URL) the contract also runs against a real Redis.

Ported from cli-server-node ``src/__tests__/config-redis.test.ts``."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.create_cli_server import CliServerConfigOptions
from qodalis_cli.models.cli_installed_plugin import CliInstalledPlugin
from qodalis_cli_config_redis import (
    DEFAULT_REDIS_CONFIG_KEY_PREFIX,
    REDIS_CONFIG_WRITE_SCRIPT,
    CliRedisConfigStore,
)
from qodalis_cli_server_abstractions.config import (
    CliConfigConflictError,
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigStoredSection,
    CliStoredConfigProvider,
    InMemoryConfigStore,
    cli_config_context,
    describe_cli_config_store_line,
)
from tests.config_store_contract import ConfigStoreContract

try:  # fakeredis runs Lua scripts only with lupa installed
    import fakeredis  # type: ignore[import-not-found]
    import lupa  # type: ignore[import-not-found]  # noqa: F401

    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


class FakeRedis:
    """The commands the store uses, in memory: HGETALL, and EVAL of the write
    script (checked to be that script) with its compare-and-set semantics.
    ``before_eval`` runs before each script, to let a test write in between
    (another server)."""

    def __init__(self, options: Optional[dict[str, Any]] = None) -> None:
        self.options = {"host": "cache", "port": 6379, "db": 0} if options is None else options
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[str] = []
        self.before_eval: Optional[Callable[[], Awaitable[None]]] = None
        self.closed = False

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        self.calls.append(f"HGETALL {key}")
        await asyncio.sleep(0)  # a round trip: other writers may run meanwhile
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        assert script == REDIS_CONFIG_WRITE_SCRIPT
        assert numkeys == 1
        hook, self.before_eval = self.before_eval, None
        if hook is not None:
            await hook()
        key, expected, values, updated_at, ttl = (str(a) for a in args)
        self.calls.append(f"EVAL {key} {expected}")
        # The script runs atomically: no await from here on
        current_hash = self.hashes.get(key, {})
        current = int(current_hash.get("version", "0"))
        if current != int(expected):
            return [0, current]
        nxt = current + 1
        self.hashes[key] = {**current_hash, "version": str(nxt), "values": values, "updatedAt": updated_at}
        if int(ttl) > 0:
            self.ttls[key] = int(ttl)
        return [1, nxt]

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# The store contract
# ---------------------------------------------------------------------------


class TestStoreContractOnAFakeRedis(ConfigStoreContract):
    @pytest.fixture
    async def store(self):
        yield CliRedisConfigStore(client=FakeRedis())


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis with lupa not installed")
class TestStoreContractOnFakeredis(ConfigStoreContract):
    @pytest.fixture
    async def store(self):
        client = fakeredis.FakeAsyncRedis()
        yield CliRedisConfigStore(client=client)
        await client.aclose()


# ---------------------------------------------------------------------------
# CliRedisConfigStore
# ---------------------------------------------------------------------------


class TestCliRedisConfigStore:
    async def test_one_hash_per_provider_section_under_the_key_prefix(self):
        redis = FakeRedis()
        store = CliRedisConfigStore(client=redis)
        assert store.key_prefix == DEFAULT_REDIS_CONFIG_KEY_PREFIX
        await store.write("jobs", "history", {"maxAge": "30d", "tags": ["a"]})
        assert list(redis.hashes) == ["qcli:config:jobs:history"]
        stored = redis.hashes["qcli:config:jobs:history"]
        assert json.loads(stored["values"]) == {"maxAge": "30d", "tags": ["a"]}
        assert stored["version"] == "1"
        assert datetime.fromisoformat(stored["updatedAt"].replace("Z", "+00:00")).tzinfo is not None
        assert stored["updatedAt"].endswith("Z")
        custom = CliRedisConfigStore(client=redis, key_prefix="app:settings/")
        await custom.write("jobs", "history", {"a": 1})
        assert custom.key_of("jobs", "history") == "app:settings/jobs:history"
        assert "app:settings/jobs:history" in redis.hashes
        assert (await store.read("jobs", "history")).version == 1

    def test_is_the_database_source_persistent(self):
        store = CliRedisConfigStore(client=FakeRedis())
        assert store.source == "database"
        assert store.describe().persistent is True

    def test_describes_itself_by_host_port_database_and_prefix(self):
        assert CliRedisConfigStore(client=FakeRedis()).describe().to_dict() == {
            "kind": "redis", "title": "Redis", "detail": "cache:6379/0, prefix qcli:config", "persistent": True,
        }
        other = CliRedisConfigStore(client=FakeRedis({"host": "redis.internal", "port": 6380, "db": 3}), key_prefix="app:")
        assert other.describe().detail == "redis.internal:6380/3, prefix app"
        assert describe_cli_config_store_line(other.describe()) == "Redis (redis.internal:6380/3, prefix app), kept across restarts"
        socket = CliRedisConfigStore(client=FakeRedis({"path": "/run/redis.sock"}), key_prefix="")
        assert socket.describe().detail == "/run/redis.sock/0"
        assert CliRedisConfigStore(client=FakeRedis({})).describe().detail == "localhost:6379/0, prefix qcli:config"

    async def test_describe_never_shows_the_password_or_user_of_a_url(self):
        store = CliRedisConfigStore(url="redis://admin:s3cr3t-pass@cache.internal:6390/4", key_prefix="qcli:cfg:")
        only = CliRedisConfigStore(url="redis://:another-secret@localhost:6379")
        try:
            described = store.describe().to_dict()
            assert described == {
                "kind": "redis", "title": "Redis", "detail": "cache.internal:6390/4, prefix qcli:cfg", "persistent": True,
            }
            assert "s3cr3t" not in json.dumps(described) and "admin" not in json.dumps(described)
            assert "another-secret" not in json.dumps(only.describe().to_dict())
            socket = CliRedisConfigStore(url="unix:///run/redis.sock?db=2")
            assert socket.describe().detail == "/run/redis.sock/2, prefix qcli:config"
            await socket.close()
        finally:
            await store.close()
            await only.close()

    def test_needs_a_client_or_a_url_and_refuses_a_bad_ttl(self):
        with pytest.raises(ValueError, match="CliRedisConfigStore needs a client or a url"):
            CliRedisConfigStore()
        with pytest.raises(ValueError, match="ttl_seconds must be a whole number of seconds"):
            CliRedisConfigStore(client=FakeRedis(), ttl_seconds=-1)
        with pytest.raises(ValueError, match="ttl_seconds"):
            CliRedisConfigStore(client=FakeRedis(), ttl_seconds=1.5)  # type: ignore[arg-type]

    async def test_a_write_with_an_expected_version_that_lost_the_race_is_a_conflict_with_the_winning_version(self):
        redis = FakeRedis()
        store = CliRedisConfigStore(client=redis)
        other = CliRedisConfigStore(client=redis)
        await store.write("p", "s", {"a": 1})

        async def theirs() -> None:
            # Between this server's read and its script, the other one writes
            await other.write("p", "s", {"a": "theirs"})

        redis.before_eval = theirs
        with pytest.raises(CliConfigConflictError) as info:
            await store.write("p", "s", {"a": "mine"}, 1)
        assert info.value.current_version == "2"
        assert await store.read("p", "s") == CliConfigStoredSection({"a": "theirs"}, 2)

    async def test_a_write_without_an_expected_version_that_lost_the_race_is_retried_on_the_new_values(self):
        redis = FakeRedis()
        store = CliRedisConfigStore(client=redis)
        other = CliRedisConfigStore(client=redis)
        await store.write("p", "s", {"a": 1})

        async def theirs() -> None:
            await other.write("p", "s", {"b": 2})

        redis.before_eval = theirs
        assert await store.write("p", "s", {"c": 3}) == CliConfigStoredSection({"a": 1, "b": 2, "c": 3}, 3)
        assert len([c for c in redis.calls if c.startswith("EVAL")]) == 4

    async def test_gives_up_after_max_retries_when_the_section_keeps_changing(self):
        redis = FakeRedis()
        store = CliRedisConfigStore(client=redis, max_retries=2)
        other = CliRedisConfigStore(client=redis)
        racing = [0]

        async def race() -> None:
            racing[0] += 1
            await other.write("p", "s", {"n": racing[0]})
            redis.before_eval = race

        redis.before_eval = race
        with pytest.raises(RuntimeError, match=r"config section p\.s: too many concurrent writes"):
            await store.write("p", "s", {"mine": True})
        redis.before_eval = None
        assert racing[0] == 3

    async def test_ttl_seconds_expires_a_section_after_its_last_change_none_by_default(self):
        redis = FakeRedis()
        await CliRedisConfigStore(client=redis).write("p", "kept", {"a": 1})
        await CliRedisConfigStore(client=redis, ttl_seconds=3600).write("p", "expiring", {"a": 1})
        assert "qcli:config:p:kept" not in redis.ttls
        assert redis.ttls["qcli:config:p:expiring"] == 3600

    async def test_unreadable_values_or_versions_read_as_empty(self):
        redis = FakeRedis()
        redis.hashes["qcli:config:p:s"] = {"values": "{oops", "version": "x"}
        redis.hashes["qcli:config:p:t"] = {"values": "[1,2]", "version": "4"}
        store = CliRedisConfigStore(client=redis)
        assert await store.read("p", "s") == CliConfigStoredSection({}, 0)
        assert await store.read("p", "t") == CliConfigStoredSection({}, 4)

    async def test_an_unexpected_script_reply_is_an_error(self):
        redis = FakeRedis()

        async def nope(*args: Any) -> Any:
            return "nope"

        redis.eval = nope  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="unexpected reply from Redis: 'nope'"):
            await CliRedisConfigStore(client=redis).write("p", "s", {"a": 1})

    async def test_close_ends_only_a_client_it_created(self):
        shared = FakeRedis()
        await CliRedisConfigStore(client=shared).close()
        assert shared.closed is False
        owned = CliRedisConfigStore(url="redis://localhost:6379/0")
        await owned.close()

    def test_is_listed_as_an_installed_plugin_package(self):
        plugin = CliInstalledPlugin.from_distribution("qodalis-cli-server-plugin-config-redis")
        assert plugin.name == "Config Redis"
        assert plugin.icon == "🔴"
        assert plugin.version is None or plugin.version.startswith("1.")


# ---------------------------------------------------------------------------
# One provider of a server
# ---------------------------------------------------------------------------


class TestCliRedisConfigStoreForOneProviderOfAServer:
    def test_a_provider_in_redis_the_rest_in_the_default_store(self):
        redis = FakeRedis({"host": "cache", "port": 6379, "db": 0})
        redis_store = CliRedisConfigStore(client=redis)
        fallback = InMemoryConfigStore()
        limits = {"maxAge": "30d"}
        provider = CliStoredConfigProvider(
            id="history",
            title="History",
            sections=[CliConfigSectionDefinition(id="main", settings=[
                CliConfigSettingDefinition(
                    key="maxAge", type="string", default="30d", apply=lambda v: limits.__setitem__("maxAge", str(v)),
                ),
            ])],
        )
        cli = create_cli_server(CliServerOptions(
            config=CliServerConfigOptions(store=fallback, stores={"history": redis_store}),
            configure=lambda b: b.add_config_provider(provider),
        ))
        context = cli_config_context(None, "api")
        with TestClient(cli.app) as client:
            views = {p["id"]: p for p in client.portal.call(cli.config.list, context)}
            assert views["history"]["store"] == {
                "kind": "redis", "title": "Redis", "detail": "cache:6379/0, prefix qcli:config", "persistent": True,
            }
            assert views["custom"]["store"] == {"kind": "memory", "title": "In memory", "detail": "", "persistent": False}
            client.portal.call(cli.config.update, "history", "main", {"changes": {"maxAge": "7d"}}, context)
            assert limits["maxAge"] == "7d"
            assert json.loads(redis.hashes["qcli:config:history:main"]["values"]) == {"maxAge": "7d"}
            assert client.portal.call(fallback.read, "history", "main") == CliConfigStoredSection({}, 0)
            res = client.post("/api/v1/qcli/execute", json={
                "command": "config", "chainCommands": ["list"], "rawCommand": "config list",
            })
            text = "\n".join(str(o.get("value")) for o in res.json().get("outputs") or [])
            assert "history  History  · Redis (cache:6379/0, prefix qcli:config)" in text


# ---------------------------------------------------------------------------
# A real Redis (QCLI_TEST_REDIS)
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("QCLI_TEST_REDIS")
RUN_PREFIX = f"qcli:test:{os.getpid()}:{int(time.time() * 1000)}:"


async def _remove_test_keys() -> None:
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(REDIS_URL)
    try:
        keys = [k async for k in client.scan_iter(match=f"{RUN_PREFIX}*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


@pytest.mark.skipif(not REDIS_URL, reason="QCLI_TEST_REDIS not set")
class TestStoreContractOnARealRedis(ConfigStoreContract):
    @pytest.fixture
    async def store(self):
        store = CliRedisConfigStore(url=REDIS_URL, key_prefix=f"{RUN_PREFIX}{uuid.uuid4().hex}:")
        try:
            yield store
        finally:
            await store.close()
            await _remove_test_keys()


@pytest.mark.skipif(not REDIS_URL, reason="QCLI_TEST_REDIS not set")
async def test_real_redis_two_stores_racing_on_one_section_no_change_lost_stale_versions_refused():
    a = CliRedisConfigStore(url=REDIS_URL, key_prefix=f"{RUN_PREFIX}race:")
    b = CliRedisConfigStore(url=REDIS_URL, key_prefix=f"{RUN_PREFIX}race:")
    try:
        await asyncio.gather(*((a if i % 2 else b).write("p", "s", {f"k{i}": i}) for i in range(10)))
        final = await a.read("p", "s")
        assert final.version == 10
        assert len(final.values) == 10
        with pytest.raises(CliConfigConflictError):
            await b.write("p", "s", {"k0": "x"}, 3)
    finally:
        await a.close()
        await b.close()
        await _remove_test_keys()
