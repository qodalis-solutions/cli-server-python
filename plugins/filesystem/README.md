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

Requests to paths outside the whitelist raise `FileStoragePermissionError`.

## License

MIT
