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

## License

MIT
