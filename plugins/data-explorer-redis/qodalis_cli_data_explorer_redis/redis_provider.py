"""Redis-backed Data Explorer provider."""

from __future__ import annotations

import time
from typing import Any

import redis.asyncio as aioredis

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)

# Commands that are safe to execute via the data explorer
_ALLOWED_COMMANDS = frozenset({
    # Key inspection
    "GET", "MGET", "GETRANGE", "STRLEN",
    "TYPE", "TTL", "PTTL", "PERSIST",
    "EXISTS", "KEYS", "SCAN",
    "OBJECT", "DEBUG",
    # Hash
    "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS",
    # List
    "LRANGE", "LLEN", "LINDEX",
    # Set
    "SMEMBERS", "SCARD", "SRANDMEMBER", "SISMEMBER",
    # Sorted set
    "ZRANGE", "ZRANGEBYSCORE", "ZRANGEBYLEX",
    "ZREVRANGE", "ZREVRANGEBYSCORE",
    "ZSCORE", "ZRANK", "ZREVRANK", "ZCARD", "ZCOUNT",
    # Stream
    "XRANGE", "XREVRANGE", "XLEN", "XREAD",
    # Server info (read-only)
    "INFO", "DBSIZE", "TIME", "PING", "CLIENT",
    # Write commands (opt-in)
    "SET", "MSET", "SETEX", "PSETEX", "SETNX",
    "DEL", "UNLINK", "RENAME", "EXPIRE", "PEXPIRE", "EXPIREAT",
    "HSET", "HMSET", "HDEL",
    "LPUSH", "RPUSH", "LPOP", "RPOP", "LSET", "LREM",
    "SADD", "SREM", "SMOVE",
    "ZADD", "ZREM", "ZINCRBY",
    "INCR", "DECR", "INCRBY", "DECRBY", "INCRBYFLOAT",
    "APPEND",
})

# Commands whose result is a flat list of field/value pairs (alternating)
_PAIR_LIST_COMMANDS = frozenset({"HGETALL"})

# Commands that return a list of items
_LIST_COMMANDS = frozenset({
    "KEYS", "HKEYS", "HVALS", "SMEMBERS", "LRANGE",
    "ZRANGE", "ZREVRANGE", "ZRANGEBYSCORE", "ZREVRANGEBYSCORE",
    "ZRANGEBYLEX", "MGET",
})

# Commands that return a single scalar value
_SCALAR_COMMANDS = frozenset({
    "GET", "HGET", "LINDEX", "SRANDMEMBER", "ZSCORE",
    "TYPE", "TTL", "PTTL", "STRLEN", "LLEN", "HLEN",
    "SCARD", "ZCARD", "ZRANK", "ZREVRANK", "ZCOUNT",
    "EXISTS", "HEXISTS", "SISMEMBER", "DBSIZE", "PING",
})


def _decode(value: Any) -> Any:
    """Recursively decode bytes to str."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        return {_decode(k): _decode(v) for k, v in value.items()}
    return value


def _normalise_result(command: str, raw: Any) -> tuple[list[str], list[list[Any]]]:
    """Normalise a raw redis-py result into (columns, rows) table form."""
    decoded = _decode(raw)

    if command in _PAIR_LIST_COMMANDS:
        # HGETALL returns a dict from redis-py (or list of pairs in older
        # versions).  Normalise both.
        if isinstance(decoded, dict):
            columns = ["field", "value"]
            rows = [[k, v] for k, v in decoded.items()]
        elif isinstance(decoded, list):
            # alternating field / value
            columns = ["field", "value"]
            rows = [
                [decoded[i], decoded[i + 1]]
                for i in range(0, len(decoded) - 1, 2)
            ]
        else:
            columns = ["value"]
            rows = [[decoded]]
        return columns, rows

    if command in _LIST_COMMANDS:
        if isinstance(decoded, list):
            columns = ["value"]
            rows = [[item] for item in decoded]
        else:
            columns = ["value"]
            rows = [[decoded]]
        return columns, rows

    if command in _SCALAR_COMMANDS:
        return ["value"], [[decoded]]

    # Generic fallback — wrap whatever we got
    if isinstance(decoded, list):
        columns = ["value"]
        rows = [[item] for item in decoded]
    elif isinstance(decoded, dict):
        columns = list(decoded.keys())
        rows = [list(decoded.values())]
    else:
        columns = ["value"]
        rows = [[decoded]]

    return columns, rows


class RedisDataExplorerProvider(IDataExplorerProvider):
    """Executes Redis commands against a Redis server."""

    def __init__(self, connection_string: str = "redis://localhost:6379") -> None:
        self._connection_string = connection_string

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        """Execute a Redis command against the server."""
        start = time.monotonic()
        client: aioredis.Redis | None = None
        try:
            parts = context.query.strip().split()
            if not parts:
                raise ValueError("Empty query — please provide a Redis command.")

            command = parts[0].upper()
            args = parts[1:]

            if command not in _ALLOWED_COMMANDS:
                raise ValueError(
                    f"Command '{command}' is not permitted. "
                    "Only safe read/write Redis commands are allowed."
                )

            client = aioredis.from_url(
                self._connection_string,
                decode_responses=False,
            )
            raw = await client.execute_command(command, *args)

            columns, rows = _normalise_result(command, raw)

            return DataExplorerResult(
                success=True,
                source=context.options.name,
                language=context.options.language,
                default_output_format=context.options.default_output_format,
                execution_time=int((time.monotonic() - start) * 1000),
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=False,
                error=None,
            )
        except Exception as exc:
            return DataExplorerResult(
                success=False,
                source=context.options.name,
                language=context.options.language,
                default_output_format=context.options.default_output_format,
                execution_time=int((time.monotonic() - start) * 1000),
                columns=None,
                rows=[],
                row_count=0,
                truncated=False,
                error=str(exc),
            )
        finally:
            if client is not None:
                await client.aclose()

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        """Scan keys and group them by Redis type to form a schema."""
        client: aioredis.Redis | None = None
        try:
            client = aioredis.from_url(
                self._connection_string,
                decode_responses=True,
            )

            keys: list[str] = []
            cursor: int = 0
            max_keys = 1000
            while True:
                cursor, batch = await client.scan(
                    cursor=cursor, count=100
                )
                keys.extend(batch)
                if cursor == 0 or len(keys) >= max_keys:
                    break
            keys = keys[:max_keys]

            type_to_keys: dict[str, list[str]] = {}
            for key in keys:
                key_type: str = await client.type(key)  # type: ignore[assignment]
                type_to_keys.setdefault(key_type, []).append(key)

            _TYPE_COLUMNS: dict[str, list[DataExplorerSchemaColumn]] = {
                "string": [
                    DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                    DataExplorerSchemaColumn(name="value", type="string", nullable=True, primary_key=False),
                ],
                "hash": [
                    DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                    DataExplorerSchemaColumn(name="field", type="string", nullable=False, primary_key=False),
                    DataExplorerSchemaColumn(name="value", type="string", nullable=True, primary_key=False),
                ],
                "list": [
                    DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                    DataExplorerSchemaColumn(name="index", type="integer", nullable=False, primary_key=False),
                    DataExplorerSchemaColumn(name="value", type="string", nullable=True, primary_key=False),
                ],
                "set": [
                    DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                    DataExplorerSchemaColumn(name="member", type="string", nullable=True, primary_key=False),
                ],
                "zset": [
                    DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                    DataExplorerSchemaColumn(name="member", type="string", nullable=False, primary_key=False),
                    DataExplorerSchemaColumn(name="score", type="float", nullable=False, primary_key=False),
                ],
                "stream": [
                    DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                    DataExplorerSchemaColumn(name="id", type="string", nullable=False, primary_key=False),
                    DataExplorerSchemaColumn(name="fields", type="object", nullable=True, primary_key=False),
                ],
            }

            tables: list[DataExplorerSchemaTable] = []
            for redis_type, type_keys in sorted(type_to_keys.items()):
                columns = _TYPE_COLUMNS.get(
                    redis_type,
                    [
                        DataExplorerSchemaColumn(name="key", type="string", nullable=False, primary_key=True),
                        DataExplorerSchemaColumn(name="value", type="string", nullable=True, primary_key=False),
                    ],
                )
                tables.append(
                    DataExplorerSchemaTable(
                        name=redis_type,
                        type="table",
                        columns=columns,
                    )
                )

            return DataExplorerSchemaResult(source=options.name, tables=tables)
        finally:
            if client is not None:
                await client.aclose()
