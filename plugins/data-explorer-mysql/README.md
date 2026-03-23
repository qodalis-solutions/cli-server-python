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

## License

MIT
