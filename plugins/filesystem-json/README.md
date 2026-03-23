# qodalis-cli-server-plugin-filesystem-json

JSON file-based storage provider for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Implements the `IFileStorageProvider` interface from the filesystem plugin, persisting all files and directories as a single JSON file on disk.

## Install

```bash
pip install qodalis-cli-server-plugin-filesystem-json
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_filesystem_json import JsonFileStorageProvider, JsonFileProviderOptions

provider = JsonFileStorageProvider(
    JsonFileProviderOptions(file_path="./data/files.json")
)

def configure(builder):
    builder.set_file_storage_provider(provider)

app = create_cli_server(configure=configure)
```

## Configuration

| Option | Type | Description |
|---|---|---|
| `file_path` | `str` | Path to the JSON file used for storage |

The JSON file is created automatically if it does not exist. Parent directories are created as needed. The file is rewritten after every write mutation.

## Dependencies

- `qodalis-cli-server-plugin-filesystem`

## License

MIT
