# qodalis-cli-server-plugin-data-explorer-elasticsearch

Elasticsearch Data Explorer provider plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Executes HTTP requests against an Elasticsearch cluster and returns structured results compatible with the Data Explorer API.

## Install

```bash
pip install qodalis-cli-server-plugin-data-explorer-elasticsearch
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    DataExplorerLanguage,
    DataExplorerOutputFormat,
)
from qodalis_cli_data_explorer_elasticsearch import ElasticsearchDataExplorerProvider

def configure(builder):
    builder.add_data_explorer_provider(
        ElasticsearchDataExplorerProvider("http://localhost:9200"),
        DataExplorerProviderOptions(
            name="elasticsearch",
            description="Elasticsearch cluster",
            language=DataExplorerLanguage.ELASTICSEARCH,
            default_output_format=DataExplorerOutputFormat.JSON,
        ),
    )

app = create_cli_server(configure=configure)
```

## Query Syntax

Queries follow the Elasticsearch HTTP API format. The first line specifies the method and path, and subsequent lines contain the optional JSON body:

```
GET /my-index/_search
{
  "query": { "match_all": {} },
  "size": 10
}
```

If no method is specified, `GET` is used by default. The `_cat` endpoints automatically append `format=json`.

## Configuration

| Parameter | Type | Default | Description |
|---|---|---|---|
| `node` | `str` | `http://localhost:9200` | Elasticsearch node URL |

## Dependencies

- `qodalis-cli-server-plugin-data-explorer`
- `elasticsearch[async]>=8.16.0`

## Terminal commands

None of its own: the provider is queried through the [data explorer plugin](../data-explorer/README.md#terminal-commands)
(`data-explorer` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install data-explorer-elasticsearch`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_data_explorer_elasticsearch import plugin as data_explorer_elasticsearch

builder.add_plugin(data_explorer_elasticsearch, {...})
```

Entry point `data-explorer-elasticsearch = "qodalis_cli_data_explorer_elasticsearch.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 70. Its settings
(provider `data-explorer-elasticsearch`, section `settings`; `config set data-explorer-elasticsearch.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `name` | string | `"elasticsearch"` | Shown in the data explorer |
| `description` | string | `"Elasticsearch database"` | Description |
| `node` | secret (required) |  | e.g. https://user:password@host:9200 (kept as a secret, never returned) |
| `readOnly` | boolean | `true` | Queries may only read |
| `timeoutMs` | number | `30000` | Timeout (ms) |
| `maxRows` | number | `1000` | Rows at most |

## License

MIT
