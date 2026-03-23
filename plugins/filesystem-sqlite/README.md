# qodalis-cli-server-plugin-filesystem-sqlite

SQLite-based storage provider for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Implements the `IFileStorageProvider` interface from the filesystem plugin, storing files and directories in a local SQLite database.

## Install

```bash
pip install qodalis-cli-server-plugin-filesystem-sqlite
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_filesystem_sqlite import SqliteFileStorageProvider, SqliteProviderOptions

provider = SqliteFileStorageProvider(
    SqliteProviderOptions(db_path="./data/files.db")
)

def configure(builder):
    builder.set_file_storage_provider(provider)

app = create_cli_server(configure=configure)
```

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `./data/files.db` | Path to the SQLite database file |

The database and parent directories are created automatically. Uses WAL journal mode for better concurrency. No external dependencies beyond Python's built-in `sqlite3` module.

## Dependencies

- `qodalis-cli-server-plugin-filesystem`

## License

MIT
