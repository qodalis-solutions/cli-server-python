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

## License

MIT
