# qodalis-cli-server-plugin-data-explorer-mongo

MongoDB Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes MongoDB queries and commands against a database and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-mongo
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    DataExplorerLanguage,
    DataExplorerOutputFormat,
)
from qodalis_cli_data_explorer_mongo import MongoDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        MongoDataExplorerProvider("mongodb://localhost:27017", "mydb"),
        DataExplorerProviderOptions(
            name="mongo",
            description="MongoDB database",
            language=DataExplorerLanguage.MONGODB,
            default_output_format=DataExplorerOutputFormat.JSON,
        ),
    )

app = create_cli_server(configure=configure)
```

## Query Syntax

Queries use MongoDB shell-style syntax:

```
show collections
show dbs
db.users.find({"active": true})
db.orders.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}}}])
```

## Configuration

| Parameter | Type | Description |
|---|---|---|
| `connection_string` | `str` | MongoDB connection URI (e.g., `mongodb://localhost:27017`) |
| `database` | `str` | Database name to query |

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`
- `motor>=3.0.0`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-mongo`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_mongo import plugin as data_explorer_mongo

builder.add_plugin(data_explorer_mongo, {...})
```

Entry point `data-explorer-mongo = "qodalis_cli_data_explorer_mongo.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-mongo`, section `settings`; `config set data-explorer-mongo.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"mongo"` | Shown in the data explorer |
| `description` | string | `"MongoDB database"` | Description |
| `connectionString` | secret (required) |  | e.g. mongodb://user:password@host:27017 (kept as a secret, never returned) |
| `database` | string (required) |  | The database queried |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
