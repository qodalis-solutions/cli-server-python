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

## License

MIT
