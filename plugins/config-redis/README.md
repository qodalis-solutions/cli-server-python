# qodalis-cli-server-plugin-config-redis

A Redis store for the [config providers](../../README.md#config-providers) of
[Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python): the writable settings of one
provider, several or all of them, kept in Redis and shared by every server using the same Redis (the Node and .NET
servers included: same keys, same hashes, same write script).

## Install

```bash
pip install qodalis-cli-server-plugin-config-redis
```

It uses [redis-py](https://github.com/redis/redis-py) (`redis>=5`, installed with it).

## Quick Start

Every provider can have its own store. Keep `jobs` in Redis and the rest in the app's settings file:

```python
import os

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.create_cli_server import CliServerConfigOptions
from qodalis_cli_server_abstractions.config import JsonFileConfigStore
from qodalis_cli_config_redis import CliRedisConfigStore

redis_store = CliRedisConfigStore(url=os.environ["REDIS_URL"])  # redis://[:password@]host:6379/0

server = create_cli_server(CliServerOptions(
    config=CliServerConfigOptions(
        store=JsonFileConfigStore("config/app.json", section="qcli"),  # the default store
        stores={"jobs": redis_store},                                   # per provider id
    ),
))
```

The stored settings are applied when the app starts (or by `await server.config.ready()`).

Other ways to give a provider this store:

```python
builder.set_config_store(redis_store)                  # the default store: every provider without its own
builder.set_config_store(redis_store, "users")         # one provider, by id (wins over the provider's own)
CliJobsBuilder().set_config_store(redis_store)         # the jobs plugin's provider (also users, portal, admin)
CliStoredConfigProvider(id="billing", title="Billing", store=redis_store, sections=[...])
```

One store may serve several providers: their data never mixes (one key per provider and section).

## Options

| Option | Default | Description |
|---|---|---|
| `url` | | A Redis URL (`redis://`, `rediss://` for TLS); the store creates its client (connecting on the first command) and `close()` ends it |
| `client` | | A `redis.asyncio` client of your app (or any object with async `hgetall` and `eval`); the store never closes it. `client` or `url` is required |
| `key_prefix` | `qcli:config:` | Prepended to every key |
| `ttl_seconds` | none | Expire a section this many seconds after its last change; without it sections are kept until deleted |
| `max_retries` | `10` | How often a write without an expected version is retried when another write came first |

## How It Works

- **One hash per provider section**: `qcli:config:<provider>:<section>` with the fields `values` (the settings
  changed from their defaults, as JSON), `version` (grows by one on every write) and `updatedAt` (ISO 8601).
- **Atomic, versioned writes**: a write reads the section, applies the changes (`None` removes a key) and stores them
  with a Lua script (`REDIS_CONFIG_WRITE_SCRIPT`) that first checks the version is still the one read. A change made
  with an expected version (the dashboard, `config set --expected-version`, `PATCH` with `version`) that is stale, or
  that loses the race to another server, is refused with `CliConfigConflictError` (HTTP 409 with the current version);
  one without is retried on the new values, so concurrent writers never lose a change.
- **Source** `database`, **persistent**: the settings survive server restarts (as long as Redis keeps its data).
- **`describe()`**: `{"kind": "redis", "title": "Redis", "detail": "cache:6379/0, prefix qcli:config", "persistent": true}`
  (host, port, database and key prefix; never the user or password), shown as the provider's `store` by
  `GET /api/v1/qcli/config` and by `config list`:

```text
jobs  Jobs  · Redis (cache:6379/0, prefix qcli:config)
```

Secrets are stored as given: protect the Redis (password, TLS, network) like the rest of the server's data.

## Tests

```bash
pip install -e plugins/config-redis/
pytest plugins/config-redis/tests/
```

The store runs the same contract as the in-memory, JSON file and database stores on a fake Redis (fakeredis when
installed, else a small in-test fake running the write script's logic). Set `QCLI_TEST_REDIS`
(`redis://localhost:6379/15`) to run it against a real Redis too, keys under a temporary prefix removed afterwards.

## Terminal commands

None of its own: the settings are read and changed with the server's [`config` command](../../README.md#permissions-audit-events-mcp-terminal)
(`config list`, `config get`, `config set`, `config reset`).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install config-redis`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_config_redis import plugin as config_redis

builder.add_plugin(config_redis, {...})
```

Entry point `config-redis = "qodalis_cli_config_redis.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 20. Its settings
(provider `config-redis`, section `settings`; `config set config-redis.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `url` | secret (required) |  | redis://[:password@]host:6379/0 (rediss:// for TLS); kept as a secret, never returned |
| `keyPrefix` | string | `"qcli:config:"` | Prepended to every key: <prefix><provider>:<section> |
| `ttlSeconds` | number | `0` | A section expires this long after its last change; 0: never |

## License

MIT
