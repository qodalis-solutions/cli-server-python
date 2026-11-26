# qodalis-cli-server-plugin-data-explorer-sql

SQLite Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes SQL queries against a SQLite database and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-sql
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import DataExplorerProviderOptions
from qodalis_cli_data_explorer_sql import SqlDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        SqlDataExplorerProvider(":memory:"),
        DataExplorerProviderOptions(
            name="sql",
            description="In-memory SQLite database for ad-hoc queries",
        ),
    )

app = create_cli_server(configure=configure)
```

## Configuration

| Parameter | Type | Default | Description |
|---|---|---|---|
| `filename` | `str` | `":memory:"` | Path to the SQLite database file, or `":memory:"` for an in-memory database |

Uses Python's built-in `sqlite3` module with no external dependencies. Supports both SELECT queries (returns rows) and write statements (returns affected row count).

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-sql`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_sql import plugin as data_explorer_sql

builder.add_plugin(data_explorer_sql, {...})
```

Entry point `data-explorer-sql = "qodalis_cli_data_explorer_sql.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-sql`, section `settings`; `config set data-explorer-sql.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"sql"` | Shown in the data explorer |
| `description` | string | `"SQLite database"` | Description |
| `filename` | string | `"data.db"` | The database file (relative paths from the plugin's data directory; :memory: for none) |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
