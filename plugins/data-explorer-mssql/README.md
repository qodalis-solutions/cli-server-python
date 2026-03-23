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

## License

MIT
