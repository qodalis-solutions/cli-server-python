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

## License

MIT
