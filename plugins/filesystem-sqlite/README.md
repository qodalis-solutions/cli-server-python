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
| `table_prefix` | `str \| None` | `Qcli` | Prefix of the table (`""` gives `Files`) |
| `tables` | `dict[str, str]` | `{}` | The table's own name: `{"Files": "MyFiles"}` |
| `legacy_table_names` | `bool` | `False` | Keep the previous release's `files` table and columns |

The database and parent directories are created automatically. Uses WAL journal mode for better concurrency. No external dependencies beyond Python's built-in `sqlite3` module.

## Table and upgrades

The files live in `QcliFiles` (columns `Id`, `Path`, `Name`, `Type`, `Content`, `Size`, `Permissions`,
`CreatedAt`, `ModifiedAt`, `ParentPath`; index `IX_QcliFiles_ParentPath`), the name every server gives the
plugin's table. The schema is versioned by the [databases plugin](../databases/README.md)'s migrations (source
`qcli-filesystem`, history in `QcliMigrations` / `QcliMigrationRuns` of the same database), applied before the
first operation: a database of a previous release has its `files` table and columns renamed, the data kept.
`legacy_table_names=True` keeps the old names instead.

To see the schema in the dashboard and the terminal, register the provider's database with the databases plugin:

```python
databases = (
    CliDatabasesBuilder()
    .add_database("files", CliMigrationDatabaseOptions(provider.connection))
    .add_source("files", provider.migration_source)
    .build()
)
result.mount_plugin(databases)
```

## Dependencies

- `qodalis-cli-server-plugin-filesystem`
- `qodalis-cli-server-plugin-databases`

## Terminal commands

None of its own: the storage is reached through the [filesystem plugin](../filesystem/README.md#terminal-commands)
(`scp` in the web CLI).

## License

MIT
