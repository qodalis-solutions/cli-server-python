# qodalis-cli-server-plugin-data-explorer

Data Explorer plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Provides a registry, executor, and REST API for querying multiple data sources through pluggable provider backends.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer
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
            description="In-memory SQLite database",
        ),
    )

app = create_cli_server(configure=configure)
```

## REST API

The Data Explorer endpoints are automatically mounted when providers are registered.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/sources` | List all registered data sources |
| POST | `/execute` | Execute a query against a data source |
| GET | `/schema?source=<name>` | Introspect schema for a data source |

### Execute Request Body

```json
{
  "source": "sql",
  "query": "SELECT * FROM users",
  "parameters": {}
}
```

## Provider Options

| Option | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | (required) | Unique source identifier |
| `description` | `str` | `""` | Human-readable description |
| `language` | `str` | `"sql"` | Query language identifier |
| `default_output_format` | `str` | `"table"` | Default output format (`table`, `json`, etc.) |
| `max_rows` | `int` | `1000` | Maximum rows returned per query |
| `timeout` | `int` | `30000` | Query timeout in milliseconds |

## Available Providers

| Package | Data Source |
|---|---|
| `qodalis-cli-server-plugin-data-explorer-sql` | SQLite |
| `qodalis-cli-server-plugin-data-explorer-postgres` | PostgreSQL |
| `qodalis-cli-server-plugin-data-explorer-mysql` | MySQL |
| `qodalis-cli-server-plugin-data-explorer-mssql` | MS SQL Server |
| `qodalis-cli-server-plugin-data-explorer-mongo` | MongoDB |
| `qodalis-cli-server-plugin-data-explorer-redis` | Redis |
| `qodalis-cli-server-plugin-data-explorer-elasticsearch` | Elasticsearch |

## License

MIT
