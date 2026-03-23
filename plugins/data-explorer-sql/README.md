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

## License

MIT
