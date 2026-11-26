# qodalis-cli-server-plugin-filesystem

Filesystem abstraction layer for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Defines the `IFileStorageProvider` interface, shared models and errors, and ships two built-in providers: in-memory and OS filesystem.

## Install

```bash
pip install qodalis-cli-server-plugin-filesystem
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_filesystem.providers import InMemoryFileStorageProvider, OsFileStorageProvider, OsProviderOptions

# In-memory provider (files lost on restart)
provider = InMemoryFileStorageProvider()

# OS provider (real filesystem, restricted to whitelisted paths)
provider = OsFileStorageProvider(
    OsProviderOptions(allowed_paths=["/tmp", "/app/data"])
)

def configure(builder):
    builder.set_file_storage_provider(provider)

app = create_cli_server(configure=configure)
```

## Built-in Providers

| Provider | Description |
|---|---|
| `InMemoryFileStorageProvider` | Tree-based in-memory storage; fast but ephemeral |
| `OsFileStorageProvider` | Delegates to the real OS filesystem with path whitelisting |

## IFileStorageProvider Interface

All providers implement the following async methods:

| Method | Description |
|---|---|
| `list(path)` | List entries in a directory |
| `read_file(path)` | Read a file as text |
| `write_file(path, content)` | Write content to a file |
| `stat(path)` | Return file or directory metadata |
| `mkdir(path, recursive)` | Create a directory |
| `remove(path, recursive)` | Remove a file or directory |
| `copy(src, dest)` | Copy a file or directory |
| `move(src, dest)` | Move (rename) a file or directory |
| `exists(path)` | Check if a path exists |
| `get_download_stream(path)` | Stream file content as async bytes |
| `upload_file(path, content)` | Upload raw bytes to a path |

## OsProviderOptions

| Option | Type | Default | Description |
|---|---|---|---|
| `allowed_paths` | `list[str]` | `[]` | Absolute directory paths clients may access |

Requests to paths outside the whitelist raise `FileStoragePermissionError`. An allowed root itself cannot be removed or moved/renamed (as source or target) either: those calls raise `FileStoragePermissionError` too (403 over HTTP), while everything inside the root can.

## Dashboard page

The admin dashboard's Filesystem page (`@qodalis/cli-dashboard-module-filesystem`) is embedded in this package
(`qodalis_cli_filesystem/dashboard`, copied at build by `scripts/copy_dashboard.py`). The server serves it at
`/qcli/modules/filesystem` and lists it (permission `files:read`) when the filesystem API is on;
`CliServerOptions(dashboard_pages={"filesystem": path})` serves another build, `False` leaves it out.

## Terminal commands

No server-side command: the web CLI drives `/api/qcli/fs/*` (`files:read` for reading, `files:write` for changes on a
protected server) through [`@qodalis/cli-scp`](https://www.npmjs.com/package/@qodalis/cli-scp):

| Command | Permission | Does |
|---|---|---|
| `scp ls <server> <path>`, `scp cat <server> <path>`, `scp stat <server> <path>` | `files:read` | Lists a directory, prints a file, shows its details |
| `scp download <server> <remote> [local]` (`get`), `scp <server>:<remote> [local]` | `files:read` | Downloads into the virtual file system (`--save`: through the browser) |
| `scp upload <server> <local> <remote>` (`put`), `scp <local> <server>:<remote>` | `files:write` | Uploads from the virtual file system (`--local`: a file you pick) |
| `scp mkdir <server> <path>`, `scp rm <server> <path>` | `files:write` | Creates a directory, deletes a file |

The `ls`, `cat`, `cd`, ... commands of `@qodalis/cli-files` work on the browser's own file system, not the server's.
The upload limit and the allowed paths are settings too: `config get filesystem` (core `config` command).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install filesystem`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_filesystem import plugin as filesystem

builder.add_plugin(filesystem, {...})
```

Entry point `filesystem = "qodalis_cli_filesystem.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 50. Its settings
(provider `filesystem-os`, section `settings`; `config set filesystem-os.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `roots` | string[] (required) | `[]` | Absolute paths served (and everything below them); none: the filesystem API serves nothing |
| `readOnly` | boolean | `true` | Only list, read and download: uploads, new folders and deletes are refused |

## License

MIT
