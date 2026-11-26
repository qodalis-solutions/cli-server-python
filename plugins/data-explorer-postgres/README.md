# qodalis-cli-server-plugin-data-explorer-postgres

PostgreSQL Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes SQL queries against a PostgreSQL database and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-postgres
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import DataExplorerProviderOptions
from qodalis_cli_data_explorer_postgres import PostgresDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        PostgresDataExplorerProvider("postgresql://user:password@localhost:5432/mydb"),
        DataExplorerProviderOptions(
            name="postgres",
            description="PostgreSQL database",
        ),
    )

app = create_cli_server(configure=configure)
```

## Connection String

Uses standard PostgreSQL URI format:

```
postgresql://user:password@host:port/database
```

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`
- `asyncpg>=0.29.0`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-postgres`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_postgres import plugin as data_explorer_postgres

builder.add_plugin(data_explorer_postgres, {...})
```

Entry point `data-explorer-postgres = "qodalis_cli_data_explorer_postgres.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-postgres`, section `settings`; `config set data-explorer-postgres.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"postgres"` | Shown in the data explorer |
| `description` | string | `"PostgreSQL database"` | Description |
| `connectionString` | secret (required) |  | e.g. postgresql://user:password@host:5432/database (kept as a secret, never returned) |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
