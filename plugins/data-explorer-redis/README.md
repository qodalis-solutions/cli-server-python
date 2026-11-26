# qodalis-cli-server-plugin-data-explorer-redis

Redis Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes Redis commands and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-redis
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    DataExplorerLanguage,
    DataExplorerOutputFormat,
)
from qodalis_cli_data_explorer_redis import RedisDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        RedisDataExplorerProvider("redis://localhost:6379"),
        DataExplorerProviderOptions(
            name="redis",
            description="Redis database",
            language=DataExplorerLanguage.REDIS,
            default_output_format=DataExplorerOutputFormat.JSON,
        ),
    )

app = create_cli_server(configure=configure)
```

## Supported Commands

Queries use standard Redis command syntax (e.g., `GET mykey`, `HGETALL myhash`). Both read and write commands are supported:

| Category | Commands |
|---|---|
| Key inspection | `GET`, `MGET`, `TYPE`, `TTL`, `EXISTS`, `KEYS`, `SCAN` |
| Hash | `HGET`, `HMGET`, `HGETALL`, `HKEYS`, `HVALS`, `HSET`, `HDEL` |
| List | `LRANGE`, `LLEN`, `LPUSH`, `RPUSH`, `LPOP`, `RPOP` |
| Set | `SMEMBERS`, `SCARD`, `SADD`, `SREM` |
| Sorted set | `ZRANGE`, `ZRANGEBYSCORE`, `ZSCORE`, `ZADD`, `ZREM` |
| String write | `SET`, `MSET`, `SETEX`, `INCR`, `DECR`, `APPEND` |
| Key mutation | `DEL`, `UNLINK`, `RENAME`, `EXPIRE` |
| Server info | `INFO`, `DBSIZE`, `TIME`, `PING` |

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`
- `redis[hiredis]>=5.0.0`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-redis`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_redis import plugin as data_explorer_redis

builder.add_plugin(data_explorer_redis, {...})
```

Entry point `data-explorer-redis = "qodalis_cli_data_explorer_redis.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-redis`, section `settings`; `config set data-explorer-redis.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"redis"` | Shown in the data explorer |
| `description` | string | `"Redis database"` | Description |
| `connectionString` | secret (required) |  | e.g. redis://:password@host:6379/0 (kept as a secret, never returned) |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
