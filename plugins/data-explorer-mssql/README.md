# qodalis-cli-server-plugin-data-explorer-mssql

MS SQL Server Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes SQL queries against a Microsoft SQL Server database and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-mssql
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import DataExplorerProviderOptions
from qodalis_cli_data_explorer_mssql import MssqlDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        MssqlDataExplorerProvider(
            "Server=localhost,1433;Database=mydb;User Id=sa;Password=secret"
        ),
        DataExplorerProviderOptions(
            name="mssql",
            description="SQL Server database",
        ),
    )

app = create_cli_server(configure=configure)
```

## Connection String

Uses ADO.NET-style connection strings:

```
Server=host,1433;Database=mydb;User Id=sa;Password=secret;TrustServerCertificate=true
```

| Key | Default | Description |
|---|---|---|
| `Server` | `localhost` | Host and optional port (comma-separated) |
| `Database` | `master` | Database name |
| `User Id` | `sa` | Login username |
| `Password` | (empty) | Login password |

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`
- `pymssql>=2.3.0`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-mssql`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_mssql import plugin as data_explorer_mssql

builder.add_plugin(data_explorer_mssql, {...})
```

Entry point `data-explorer-mssql = "qodalis_cli_data_explorer_mssql.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-mssql`, section `settings`; `config set data-explorer-mssql.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"mssql"` | Shown in the data explorer |
| `description` | string | `"SQL Server database"` | Description |
| `connectionString` | secret (required) |  | e.g. Server=host,1433;Database=db;User Id=user;Password=password; (kept as a secret, never returned) |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
