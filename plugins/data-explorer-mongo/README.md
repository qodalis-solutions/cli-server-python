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

## License

MIT
