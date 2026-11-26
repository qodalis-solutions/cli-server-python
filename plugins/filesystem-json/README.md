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

## Terminal commands

None of its own: the storage is reached through the [filesystem plugin](../filesystem/README.md#terminal-commands)
(`scp` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install filesystem-json`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_filesystem_json import plugin as filesystem_json

builder.add_plugin(filesystem_json, {...})
```

Entry point `filesystem-json = "qodalis_cli_filesystem_json.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 50. Its settings
(provider `filesystem-json`, section `settings`; `config set filesystem-json.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `file` | string | `"files.json"` | The file the tree is kept in (relative paths from the plugin's data directory) |
| `readOnly` | boolean | `true` | Uploads, new folders and deletes are refused |

## License

MIT
