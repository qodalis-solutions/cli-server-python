# Qodalis CLI Server (Python)

A Python CLI server framework for the [Qodalis CLI](https://github.com/qodalis-solutions/web-cli) ecosystem. Build custom server-side commands that integrate with the Qodalis web terminal.

## Installation

```bash
pip install qodalis-cli-server
```

The package exports all types, base classes, and built-in processors. Full type hints included.

Requires Python 3.10+.

### Plugin Authors

If you're building a command processor plugin and don't need the server runtime (FastAPI, uvicorn, websockets), install the abstractions package instead:

```bash
pip install qodalis-cli-server-abstractions
```

This gives you `CliCommandProcessor`, `CliProcessCommand`, `CliCommandParameterDescriptor`, and all other base types with **zero dependencies**. See [`qodalis-cli-server-abstractions`](https://pypi.org/project/qodalis-cli-server-abstractions/) for details.

## Quick Start

### As a Library

```python
from qodalis_cli import (
    CliCommandProcessor,
    CliProcessCommand,
    CliServerOptions,
    create_cli_server,
)
import uvicorn


class GreetProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "greet"

    @property
    def description(self) -> str:
        return "Says hello"

    async def handle_async(self, command: CliProcessCommand) -> str:
        name = command.value or "World"
        return f"Hello, {name}!"


result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: builder.add_processor(GreetProcessor()),
    )
)

uvicorn.run(result.app, host="0.0.0.0", port=8048)
```

Disconnect broadcast is handled automatically via the lifespan shutdown event.

### As a Standalone Server

```bash
qodalis-cli-server
```

Or with environment variables:

```bash
PORT=9000 HOST=127.0.0.1 qodalis-cli-server
```

## Creating Custom Command Processors

### Simple Command

Extend `CliCommandProcessor` and implement `command`, `description`, and `handle_async`:

```python
from qodalis_cli import CliCommandProcessor, CliProcessCommand


class EchoProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes input text back"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return command.value or "Usage: echo <text>"
```

Register it during server creation:

```python
result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: (
            builder
            .add_processor(EchoProcessor())
            .add_processor(AnotherProcessor())  # fluent chaining
        ),
    )
)
```

### Command with Parameters

Declare parameters with names, types, aliases, and defaults. The CLI client uses this metadata for autocompletion and validation.

```python
from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
)


class TimeProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "time"

    @property
    def description(self) -> str:
        return "Shows the current server time"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="utc",
                description="Show UTC time",
                type="boolean",
            ),
            CliCommandParameterDescriptor(
                name="format",
                description="Date/time format string",
                type="string",
                aliases=["-f"],
                default_value="%Y-%m-%d %H:%M:%S",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        from datetime import datetime, timezone

        use_utc = "utc" in command.args
        fmt = command.args.get("format", "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc) if use_utc else datetime.now()
        label = "UTC" if use_utc else "Local"
        return f"{label}: {now.strftime(fmt)}"
```

Parameter types: `"string"`, `"number"`, `"boolean"`.

### Sub-commands

Nest processors to create command hierarchies like `math add --a 5 --b 3`:

```python
from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)


class _MathAddProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "Adds two numbers"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="a", description="First number",
                required=True, type="number",
            ),
            CliCommandParameterDescriptor(
                name="b", description="Second number",
                required=True, type="number",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        a = float(command.args.get("a", 0))
        b = float(command.args.get("b", 0))
        result = a + b
        return f"{a} + {b} = {result}"


class _MathMultiplyProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "multiply"

    @property
    def description(self) -> str:
        return "Multiplies two numbers"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="a", description="First number",
                required=True, type="number",
            ),
            CliCommandParameterDescriptor(
                name="b", description="Second number",
                required=True, type="number",
            ),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        a = float(command.args.get("a", 0))
        b = float(command.args.get("b", 0))
        result = a * b
        return f"{a} * {b} = {result}"


class MathProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "math"

    @property
    def description(self) -> str:
        return "Performs basic math operations"

    @property
    def allow_unlisted_commands(self) -> bool:
        return False

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_MathAddProcessor(), _MathMultiplyProcessor()]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Usage: math add|multiply --a <number> --b <number>"
```

## Modules

Modules group related command processors into a reusable unit. Implement `ICliModule` (or extend the `CliModule` base class) to bundle processors under a single name and version.

### Defining a Module

```python
from qodalis_cli import (
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
    ICliCommandProcessor,
)


class _WeatherCurrentProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "current"

    @property
    def description(self) -> str:
        return "Shows current weather conditions"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Weather: Sunny, 22°C"


class _CliWeatherCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return "Shows weather information for a location"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_WeatherCurrentProcessor()]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Weather: Sunny, 22°C"


class WeatherModule(CliModule):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Provides weather information commands"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [_CliWeatherCommandProcessor()]
```

### Registering a Module

```python
result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: builder.add_module(WeatherModule()),
    )
)
```

`add_module()` iterates over the module's `processors` and registers each one, just like calling `add_processor()` for each individually.

### ICliModule Interface

| Property | Type | Description |
|----------|------|-------------|
| `name` | `str` | Unique module identifier |
| `version` | `str` | Module version |
| `description` | `str` | Short description |
| `author` | `ICliCommandAuthor` | Author metadata (defaults to library author) |
| `processors` | `Sequence[ICliCommandProcessor]` | Command processors provided by the module |

### Example: Weather Module

The repository includes a weather module under `plugins/weather/` as a reference implementation. It registers a `weather` command with `current` and `forecast` sub-commands, using the [wttr.in](https://wttr.in) API:

```
weather                    # Shows current weather (default: London)
weather current London     # Current conditions for London
weather forecast --location Paris  # 3-day forecast for Paris
```

## Command Input

Every processor receives a `CliProcessCommand` with the parsed command input:

| Property | Type | Description |
|----------|------|-------------|
| `command` | `str` | Command name (e.g., `"time"`) |
| `value` | `str \| None` | Positional argument (e.g., `"hello"` in `echo hello`) |
| `args` | `dict[str, Any]` | Named parameters (e.g., `--format "%H:%M"`) |
| `chain_commands` | `list[str]` | Sub-command chain (e.g., `["add"]` in `math add`) |
| `raw_command` | `str` | Original unprocessed input |
| `data` | `Any` | Arbitrary data payload from the client |

## API Versioning

Processors declare which API version they target. The default is version 1.

```python
class DashboardProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "dashboard"

    @property
    def description(self) -> str:
        return "Server dashboard (v2 only)"

    @property
    def api_version(self) -> int:
        return 2

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Dashboard data..."
```

The server exposes versioned endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cli/versions` | Version discovery (supported versions, preferred version) |
| GET | `/api/v1/cli/version` | V1 server version |
| GET | `/api/v1/cli/commands` | V1 commands (all processors) |
| POST | `/api/v1/cli/execute` | V1 execute |
| GET | `/api/v2/cli/version` | V2 server version |
| GET | `/api/v2/cli/commands` | V2 commands (only `api_version >= 2`) |
| POST | `/api/v2/cli/execute` | V2 execute |
| WS | `/ws/cli/events` | WebSocket events (also `/ws/v1/cli/events`, `/ws/v2/cli/events`) |

The Qodalis CLI client auto-negotiates the highest mutually supported version via the `/api/cli/versions` discovery endpoint.

## Processor Base Class Reference

`CliCommandProcessor` provides these overridable properties:

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `command` | `str` | (required) | Command name |
| `description` | `str` | (required) | Help text shown to users |
| `handle_async` | method | (required) | Execution logic |
| `parameters` | `list[ICliCommandParameterDescriptor] \| None` | `None` | Declared parameters |
| `processors` | `list[ICliCommandProcessor] \| None` | `None` | Sub-commands |
| `allow_unlisted_commands` | `bool \| None` | `None` | Accept sub-commands not in `processors` |
| `value_required` | `bool \| None` | `None` | Require a positional value |
| `version` | `str` | `"1.0.0"` | Processor version string |
| `api_version` | `int` | `1` | Target API version |
| `author` | `ICliCommandAuthor` | default author | Author metadata (name, email) |

## Server Options

```python
@dataclass
class CliServerOptions:
    base_path: str = "/api/cli"            # API base path
    cors: bool = True                       # Enable CORS
    cors_origins: list[str] = ["*"]         # Allowed origins
    configure: Callable[[CliBuilder], None] | None = None  # Processor registration
```

`create_cli_server()` returns:

```python
@dataclass
class CliServerResult:
    app: FastAPI                            # Configured FastAPI app
    registry: CliCommandRegistry            # Processor registry
    builder: CliBuilder                     # Registration builder
    event_socket_manager: CliEventSocketManager  # WebSocket manager
```

## Exported Types

All types are exported from the `qodalis_cli` package root:

```python
# Abstractions (for creating custom processors and modules)
from qodalis_cli import (
    ICliCommandProcessor,
    CliCommandProcessor,
    ICliCommandParameterDescriptor,
    CliCommandParameterDescriptor,
    CliProcessCommand,
    ICliCommandAuthor,
    CliCommandAuthor,
    ICliModule,
    CliModule,
)

# Models
from qodalis_cli import (
    CliServerResponse,
    CliServerOutput,
    CliServerCommandDescriptor,
)

# Services (for advanced integration)
from qodalis_cli import (
    ICliCommandRegistry,
    CliCommandRegistry,
    ICliCommandExecutorService,
    CliCommandExecutorService,
    ICliResponseBuilder,
    CliResponseBuilder,
    CliEventSocketManager,
)

# Factory
from qodalis_cli import (
    create_cli_server,
    CliServerOptions,
)
```

## File Storage

The server includes a pluggable file storage system exposed at `/api/cli/fs/*`. Enable it with `set_file_storage_provider()` and choose a storage backend.

### Filesystem API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cli/fs/ls?path=/` | List directory contents |
| GET | `/api/cli/fs/cat?path=/file.txt` | Read file content |
| GET | `/api/cli/fs/stat?path=/file.txt` | File/directory metadata |
| GET | `/api/cli/fs/download?path=/file.txt` | Download file |
| POST | `/api/cli/fs/upload` | Upload file (multipart) |
| POST | `/api/cli/fs/mkdir` | Create directory |
| DELETE | `/api/cli/fs/rm?path=/file.txt` | Delete file or directory |

### Storage Providers

```python
from qodalis_cli import create_cli_server, CliServerOptions
from qodalis_cli_filesystem import InMemoryFileStorageProvider, OsFileStorageProvider
from qodalis_cli_filesystem_json import JsonFileStorageProvider, JsonFileProviderOptions
from qodalis_cli_filesystem_sqlite import SqliteFileStorageProvider, SqliteProviderOptions
from qodalis_cli_filesystem_s3 import S3FileStorageProvider, S3ProviderOptions

result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: (
            # In-memory (default) — files lost on restart
            builder.set_file_storage_provider(InMemoryProvider())

            # OS filesystem
            # builder.set_file_storage_provider(OsProvider())

            # JSON file — persists to a single JSON file
            # builder.set_file_storage_provider(
            #     JsonFileStorageProvider(JsonFileProviderOptions(
            #         file_path='./data/files.json',
            #     ))
            # )

            # SQLite — persists to a SQLite database
            # builder.set_file_storage_provider(
            #     SqliteFileStorageProvider(SqliteProviderOptions(
            #         db_path='./data/files.db',
            #     ))
            # )

            # Amazon S3
            # builder.set_file_storage_provider(
            #     S3FileStorageProvider(S3ProviderOptions(
            #         bucket='my-cli-files',
            #         region='us-east-1',
            #         prefix='uploads/',
            #         aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
            #         aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
            #     ))
            # )
        ),
    )
)
```

### Custom Provider

Implement `IFileStorageProvider` to add your own backend:

```python
from qodalis_cli_filesystem import IFileStorageProvider, FileEntry, FileStat

class MyProvider(IFileStorageProvider):
    @property
    def name(self) -> str:
        return "my-provider"

    async def list(self, path: str) -> list[FileEntry]: ...
    async def read_file(self, path: str) -> str: ...
    async def write_file(self, path: str, content: str | bytes) -> None: ...
    async def stat(self, path: str) -> FileStat: ...
    async def mkdir(self, path: str, recursive: bool = False) -> None: ...
    async def remove(self, path: str, recursive: bool = False) -> None: ...
    async def copy(self, src: str, dest: str) -> None: ...
    async def move(self, src: str, dest: str) -> None: ...
    async def exists(self, path: str) -> bool: ...
    async def get_download_stream(self, path: str) -> AsyncIterator[bytes]: ...
    async def upload_file(self, path: str, content: bytes) -> None: ...

builder.set_file_storage_provider(MyProvider())
```

## Built-in Processors

These processors ship with the library and are included in the standalone server:

| Command | Description |
|---------|-------------|
| `echo` | Echoes input text |
| `status` | Server status (uptime, OS info) |
| `system` | Detailed system information (hostname, CPU, memory) |
| `http` | HTTP request operations |
| `hash` | Hash computation (MD5, SHA1, SHA256, SHA512) |
| `base64` | Base64 encode/decode (sub-commands) |
| `uuid` | UUID generation |

## Docker

```bash
docker run -p 8048:8048 ghcr.io/qodalis-solutions/cli-server-python
```

## Demo

```bash
cd demo
pip install -r requirements.txt
python main.py
# Server starts on http://localhost:8048
```

## Testing

```bash
pip install -e ".[test]"
pytest              # Run test suite
pytest -v           # Verbose output
pytest --tb=short   # Short tracebacks
```

## Project Structure

```
packages/
  abstractions/                       # qodalis-cli-server-abstractions (zero-dep)
    src/qodalis_cli_server_abstractions/
      cli_command_processor.py        # ICliCommandProcessor ABC & base class
      cli_module.py                   # ICliModule ABC & base class
      cli_process_command.py          # Command input dataclass
      cli_command_parameter_descriptor.py  # Parameter declaration
      cli_command_author.py           # Author metadata
plugins/
  filesystem/                         # Core file storage abstraction (IFileStorageProvider, InMemory, OS)
  filesystem-json/                    # JSON file persistence provider
  filesystem-sqlite/                  # SQLite persistence provider (stdlib sqlite3)
  filesystem-s3/                      # Amazon S3 storage provider (boto3)
  weather/                            # Weather module (example plugin)
src/qodalis_cli/
  abstractions/                       # Re-exports from qodalis_cli_server_abstractions
  models/
    cli_server_response.py            # Response wrapper (exitCode + outputs)
    cli_server_output.py              # Output types (text, table, list, json, key-value)
    cli_server_command_descriptor.py  # Command metadata for /commands endpoint
  services/
    cli_command_registry.py           # Processor registry and lookup
    cli_command_executor_service.py   # Command execution pipeline
    cli_response_builder.py           # Structured output builder
    cli_event_socket_manager.py       # WebSocket event broadcasting
  controllers/
    cli_controller.py                 # V1 REST API (/api/v1/cli)
    cli_controller_v2.py              # V2 REST API (/api/v2/cli)
    cli_version_controller.py         # Version discovery (/api/cli/versions)
  extensions/
    cli_builder.py                    # Fluent registration API (add_processor, add_module)
  processors/                         # Built-in processors
  create_cli_server.py               # Factory function
  server.py                          # Standalone CLI entry point
  __init__.py                        # Package exports
demo/                                # Demo app with sample processors
tests/                               # Test suite (pytest)
```

## License

MIT
