# qodalis-cli-server-plugin-data-explorer-mysql

MySQL Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes SQL queries against a MySQL database and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-mysql
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import DataExplorerProviderOptions
from qodalis_cli_data_explorer_mysql import MysqlDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        MysqlDataExplorerProvider("mysql://root:password@localhost:3306/mydb"),
        DataExplorerProviderOptions(
            name="mysql",
            description="MySQL database",
        ),
    )

app = create_cli_server(configure=configure)
```

## Connection String

Uses standard MySQL URI format:

```
mysql://user:password@host:port/database
```

| Component | Default | Description |
|---|---|---|
| `host` | `localhost` | MySQL server hostname |
| `port` | `3306` | MySQL server port |
| `user` | `root` | Login username |
| `password` | (empty) | Login password |
| `database` | (none) | Database name |

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`
- `aiomysql>=0.2.0`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-mysql`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_mysql import plugin as data_explorer_mysql

builder.add_plugin(data_explorer_mysql, {...})
```

Entry point `data-explorer-mysql = "qodalis_cli_data_explorer_mysql.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-mysql`, section `settings`; `config set data-explorer-mysql.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"mysql"` | Shown in the data explorer |
| `description` | string | `"MySQL database"` | Description |
| `connectionString` | secret (required) |  | e.g. mysql://user:password@host:3306/database (kept as a secret, never returned) |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
