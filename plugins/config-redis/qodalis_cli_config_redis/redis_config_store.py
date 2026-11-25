"""Writable config settings kept in Redis: one hash per provider section."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from qodalis_cli_server_abstractions.config import (
    CliConfigConflictError,
    CliConfigStoreDescription,
    CliConfigStoredSection,
    CliConfigValue,
    ICliConfigStore,
    apply_cli_config_changes,
)

#: The key prefix when none is given: ``qcli:config:<provider>:<section>``.
DEFAULT_REDIS_CONFIG_KEY_PREFIX = "qcli:config:"

#: The atomic write: sets the section's values and the next version only when
#: the stored version is still the one the change was computed from
#: (``ARGV[1]``); answers ``{1, newVersion}``, or ``{0, currentVersion}`` when
#: another write came first.  ``ARGV[2]`` the values as JSON, ``ARGV[3]`` the
#: time (ISO 8601), ``ARGV[4]`` the TTL in seconds (0: none).  The same script
#: as the Node and .NET servers', so they can share one Redis.
REDIS_CONFIG_WRITE_SCRIPT = """
local current = tonumber(redis.call('HGET', KEYS[1], 'version') or '0') or 0
if current ~= tonumber(ARGV[1]) then
    return {0, current}
end
local nextVersion = current + 1
redis.call('HSET', KEYS[1], 'version', nextVersion, 'values', ARGV[2], 'updatedAt', ARGV[3])
local ttl = tonumber(ARGV[4]) or 0
if ttl > 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
end
return {1, nextVersion}
"""


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


def _parse_hash(raw: Optional[Mapping[Any, Any]]) -> CliConfigStoredSection:
    """A section from its hash: ``({}, 0)`` when missing; unreadable values read as empty."""
    if not raw:
        return CliConfigStoredSection()
    fields = {_text(k): _text(v) for k, v in raw.items()}
    values: dict[str, CliConfigValue] = {}
    try:
        parsed = json.loads(fields.get("values", "{}"))
        if isinstance(parsed, dict):
            values = parsed
    except ValueError:
        values = {}
    try:
        version = int(fields.get("version", ""))
    except ValueError:
        version = 0
    return CliConfigStoredSection(values, version if version >= 0 else 0)


class CliRedisConfigStore(ICliConfigStore):
    """Keeps writable config settings in Redis (source ``database``): one hash
    per provider section (``qcli:config:<provider>:<section>``) with the
    fields ``values`` (JSON), ``version`` and ``updatedAt``.  A write reads
    the section, applies the changes and stores them with a Lua script that
    checks the version first (:data:`REDIS_CONFIG_WRITE_SCRIPT`), so servers
    sharing the Redis never lose a change: a stale expected version is
    refused (:class:`CliConfigConflictError`), a write without one is retried.

    Use it for every provider, or for some only, next to other stores::

        redis_store = CliRedisConfigStore(url=os.environ["REDIS_URL"])
        create_cli_server(CliServerOptions(config=CliServerConfigOptions(
            store=JsonFileConfigStore("config.json"), stores={"jobs": redis_store},
        )))

    ``client``: a connected ``redis.asyncio`` client shared with the app (the
    store never closes it); or ``url`` (``redis://[:password@]host:6379/0``,
    ``rediss://`` for TLS): the store creates its client and :meth:`close`
    ends it.  ``key_prefix`` is prepended to every key (default
    ``qcli:config:``); ``ttl_seconds`` expires a section that long after its
    last change (default none); ``max_retries``: how often a write without an
    expected version is retried when another write came first (default 10)."""

    source = "database"

    def __init__(
        self,
        client: Any = None,
        url: Optional[str] = None,
        key_prefix: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        max_retries: int = 10,
    ) -> None:
        if client is not None:
            self.client = client
            self._owns_client = False
        elif url:
            import redis.asyncio as aioredis

            # Connects on the first command
            self.client = aioredis.Redis.from_url(url)
            self._owns_client = True
        else:
            raise ValueError("CliRedisConfigStore needs a client or a url")
        self.key_prefix = DEFAULT_REDIS_CONFIG_KEY_PREFIX if key_prefix is None else key_prefix
        if ttl_seconds is not None and (
            not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds < 0
        ):
            raise ValueError("ttl_seconds must be a whole number of seconds")
        self.ttl_seconds = ttl_seconds or 0
        self.max_retries = max(0, max_retries)

    def _connection(self) -> dict[str, Any]:
        """Where the client connects (its pool's connection arguments); never shown whole."""
        pool = getattr(self.client, "connection_pool", None)
        kwargs = getattr(pool, "connection_kwargs", None)
        if isinstance(kwargs, Mapping):
            return dict(kwargs)
        options = getattr(self.client, "options", None)
        return dict(options) if isinstance(options, Mapping) else {}

    def describe(self) -> CliConfigStoreDescription:
        """``cache:6379/0, prefix qcli:config``: host, port, database and key
        prefix; never the password or user."""
        o = self._connection()
        where = o.get("path") or f"{o.get('host') or 'localhost'}:{o.get('port') or 6379}"
        prefix = self.key_prefix.rstrip(":")
        detail = f"{where}/{o.get('db') or 0}{f', prefix {prefix}' if prefix else ''}"
        return CliConfigStoreDescription(kind="redis", title="Redis", detail=detail, persistent=True)

    def key_of(self, provider: str, section: str) -> str:
        """The section's key: ``<prefix><provider>:<section>``."""
        return f"{self.key_prefix}{provider}:{section}"

    async def read(self, provider: str, section: str) -> CliConfigStoredSection:
        return _parse_hash(await self.client.hgetall(self.key_of(provider, section)))

    async def write(
        self,
        provider: str,
        section: str,
        changes: Mapping[str, Optional[CliConfigValue]],
        expected_version: Optional[int] = None,
    ) -> CliConfigStoredSection:
        key = self.key_of(provider, section)
        attempt = 0
        while True:
            current = await self.read(provider, section)
            if expected_version is not None and expected_version != current.version:
                raise CliConfigConflictError(str(current.version))
            values = apply_cli_config_changes(current.values, changes)
            reply = await self.client.eval(
                REDIS_CONFIG_WRITE_SCRIPT,
                1,
                key,
                current.version,
                json.dumps(values, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                self.ttl_seconds,
            )
            ok, version = _reply(reply)
            if ok == 1:
                return CliConfigStoredSection(copy.deepcopy(values), version)
            if ok != 0:
                raise RuntimeError(f"unexpected reply from Redis: {reply!r}")
            # Another write came first
            if expected_version is not None:
                raise CliConfigConflictError(str(version))
            if attempt >= self.max_retries:
                raise RuntimeError(f"config section {provider}.{section}: too many concurrent writes")
            attempt += 1

    async def close(self) -> None:
        """Ends the client the store created from ``url``; a client given to it is left open."""
        if not self._owns_client:
            return
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            await close()


def _reply(reply: Any) -> tuple[Optional[int], Optional[int]]:
    """The script's ``{ok, version}``; ``(None, None)`` for anything else."""
    if not isinstance(reply, (list, tuple)) or len(reply) != 2:
        return None, None
    try:
        return int(reply[0]), int(reply[1])
    except (TypeError, ValueError):
        return None, None
