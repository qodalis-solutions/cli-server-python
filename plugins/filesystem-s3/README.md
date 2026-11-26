# qodalis-cli-server-plugin-filesystem-s3

AWS S3-based storage provider for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Implements the `IFileStorageProvider` interface from the filesystem plugin, storing files and directories in an S3 bucket.

## Install

```bash
pip install qodalis-cli-server-plugin-filesystem-s3
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_filesystem_s3 import S3FileStorageProvider, S3ProviderOptions

provider = S3FileStorageProvider(
    S3ProviderOptions(
        bucket="my-cli-files",
        region="us-east-1",
    )
)

def configure(builder):
    builder.set_file_storage_provider(provider)

app = create_cli_server(configure=configure)
```

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `bucket` | `str` | (required) | S3 bucket name |
| `region` | `str \| None` | `None` | AWS region |
| `prefix` | `str \| None` | `None` | Key prefix for all objects (acts as a virtual root directory) |
| `endpoint_url` | `str \| None` | `None` | Custom S3 endpoint (for MinIO, LocalStack, etc.) |
| `aws_access_key_id` | `str \| None` | `None` | AWS access key (falls back to default credential chain) |
| `aws_secret_access_key` | `str \| None` | `None` | AWS secret key (falls back to default credential chain) |

Directories are virtual: `mkdir` creates a zero-byte object with a trailing `/` as a directory marker. All async methods wrap synchronous boto3 calls via `run_in_executor`.

## Dependencies

- `qodalis-cli-server-plugin-filesystem`
- `boto3>=1.26.0`

## Terminal commands

None of its own: the storage is reached through the [filesystem plugin](../filesystem/README.md#terminal-commands)
(`scp` in the web CLI).

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install filesystem-s3`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_filesystem_s3 import plugin as filesystem_s3

builder.add_plugin(filesystem_s3, {...})
```

Entry point `filesystem-s3 = "qodalis_cli_filesystem_s3.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 50. Its settings
(provider `filesystem-s3`, section `settings`; `config set filesystem-s3.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `bucket` | string (required) |  | Bucket |
| `region` | string | `"us-east-1"` | Region |
| `prefix` | string |  | Prepended to every path |
| `endpoint` | string |  | An S3-compatible service (MinIO); empty: AWS |
| `accessKeyId` | secret |  | Empty: the AWS default credential chain |
| `secretAccessKey` | secret |  | Secret access key |
| `readOnly` | boolean | `true` | Uploads, new folders and deletes are refused |

## License

MIT
