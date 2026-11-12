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
| GET | `/sources` | The sources the caller may use, with language, output format, templates, `readOnly` and `canExport` |
| POST | `/execute` | Execute a query against a data source (403 without the source's permissions, 429 over its rate limit) |
| GET | `/schema?source=<name>` | A source's schema: allowed tables only, without dropped columns (403 without the source's permissions) |

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
| `required_permissions` | `list[str]` | -- | Permissions the caller needs besides `data:query`; other callers do not see the source (ignored on an open server) |
| `read_only` | `bool` | `True` | Only queries that read are accepted, and SQL providers run them in a read-only transaction. Set `False` for a source that must write |
| `allowed_objects` | `list[str]` | all | Glob patterns of the tables, collections, Redis keys or indices the source shows and accepts |
| `mask_columns` | `list[str]` | -- | Glob patterns of columns/fields whose values are shown as `***` |
| `drop_columns` | `list[str]` | -- | Glob patterns of columns/fields removed from results and the schema |
| `audit` | `bool` | `True` | Writes a `data_query` entry to the server's audit log for every execute |
| `rate_limit` | `DataExplorerRateLimit(max_queries, window_seconds)` | -- | Executes allowed per caller and window; more answer 429 |
| `export_permissions` | `list[str]` | -- | Permissions needed to export results (`canExport` in `/sources`; advisory) |

Over HTTP the options keep the camelCase names of the other servers (`readOnly`, `canExport`, `errorCode`).

### Read-only sources

Sources are read-only unless they set `read_only=False`. The server checks each query before the provider runs it:

- SQL: one statement starting with `SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `DESCRIBE`, `VALUES` or `TABLE`, without write keywords (comments and strings ignored).
- MongoDB (`shell` or `json` language): `show collections` or `db.<collection>.find|findOne|countDocuments|estimatedDocumentCount|distinct|aggregate(...)` without `$out`/`$merge`.
- Redis: read commands only (`GET`, `HGETALL`, `LRANGE`, `KEYS`, `SCAN`, ...).
- Elasticsearch: `GET`/`HEAD`, or `POST` to `_search`, `_count`, `_msearch`, `_mget`, `_field_caps`, `_validate/query`.
- Other languages are refused unless the provider sets `read_only_queries = True` (it can only read).

The SQL providers also run the query in a read-only transaction (SQLite `PRAGMA query_only`, Postgres `BEGIN READ ONLY`, MySQL `START TRANSACTION READ ONLY`, SQL Server a transaction that is always rolled back).

`allowed_objects` and the read-only check are a convenience filter. Give the source a database account that can only read what staff may see: that account is the real boundary.

```python
from qodalis_cli_server_abstractions import DataExplorerProviderOptions, DataExplorerRateLimit

builder.add_data_explorer_provider(
    provider,
    DataExplorerProviderOptions(
        name="billing",
        description="Billing database",
        required_permissions=["data:billing"],
        allowed_objects=["public.invoices", "public.customers"],
        mask_columns=["email", "*phone*"],
        drop_columns=["password_hash"],
        rate_limit=DataExplorerRateLimit(max_queries=30, window_seconds=60),
        export_permissions=["data:export"],
    ),
)
```

Queries run in order: permissions, rate limit (per user id, or client address when anonymous), read-only check, allowed objects, the provider, `max_rows`, masking, then the audit entry (refused queries are audited too, with the refusal as `error`). A failing audit log never fails the query.

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
