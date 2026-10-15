# Qodalis CLI Server (Python)

[![PyPI qodalis-cli-server](https://img.shields.io/pypi/v/qodalis-cli-server?label=qodalis-cli-server)](https://pypi.org/project/qodalis-cli-server/)
[![PyPI qodalis-cli-server-abstractions](https://img.shields.io/pypi/v/qodalis-cli-server-abstractions?label=qodalis-cli-server-abstractions)](https://pypi.org/project/qodalis-cli-server-abstractions/)

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
| GET | `/api/qcli/versions` | Version discovery (supported versions, preferred version) |
| GET | `/api/v1/qcli/version` | V1 server version |
| GET | `/api/v1/qcli/commands` | V1 commands (all processors) |
| POST | `/api/v1/qcli/execute` | V1 execute |
| GET | `/api/v1/qcli/capabilities` | Server capabilities (OS, shell, features) |
| WS | `/ws/v1/qcli/events` | WebSocket server-push event broadcast |
| WS | `/ws/v1/qcli/logs` | WebSocket log streaming (supports `?level=` filter) |
| WS | `/ws/v1/qcli/shell` | Interactive PTY shell session (supports `?cols=&rows=&cmd=`); opt-in via `enable_shell=True`, see [Remote Shell Security](#remote-shell-security) |

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
    base_path: str = "/api/qcli"           # API base path
    cors: bool | None = None                # None: CORS only when origins are configured
    cors_origins: list[str] | None = None   # Explicit CORS origins (default: allowed_origins)
    allowed_origins: list[str] | None = None  # CORS + WebSocket Origin check ("*" globs allowed)
    enable_shell: bool = False              # PTY shell WebSocket (opt-in, see Remote Shell Security)
    shell: CliShellOptions = CliShellOptions()  # Shell access, limits and environment
    configure: Callable[[CliBuilder], None] | None = None  # Processor registration
```

CORS is disabled unless `allowed_origins` (or `cors_origins` / `cors=True`) is set, and `*` is never combined with credentials. When `allowed_origins` is set, a WebSocket upgrade whose `Origin` is not allowed is closed with code 4403 (a missing `Origin`, as sent by non-browser clients, is accepted; protected sockets still need a ticket).

Same-origin requests (an `Origin` equal to the server's own scheme + host, e.g. the bundled admin dashboard) are always allowed by every Origin/CSRF check, so a deployment never needs to list its own origin (`X-Forwarded-Proto`/`X-Forwarded-Host` are honoured only with `trust_proxy`).

Processors may declare `required_permissions` (e.g. `["deploy:run"]`); they are enforced when the server is protected by the users plugin.

`create_cli_server()` returns:

```python
@dataclass
class CliServerResult:
    app: FastAPI                            # Configured FastAPI app
    registry: CliCommandRegistry            # Processor registry
    builder: CliBuilder                     # Registration builder
    event_socket_manager: CliEventSocketManager  # WebSocket event manager
    log_socket_manager: CliLogSocketManager  # WebSocket log manager
    executor: CliCommandExecutorService | None  # Command executor service
    shell_session_manager: CliShellSessionManager | None  # PTY shell sessions

    def mount_plugin(self, plugin: object) -> None: ...  # Mount a plugin with router/dashboard
    def set_shell_access_provider(self, provider) -> None: ...  # Called by auth plugins (users mode)
```

## Remote Shell Security

The PTY shell (`/ws/v1/qcli/shell`, `/ws/qcli/shell`) gives OS-level access. It is **off** unless `enable_shell=True`, and once enabled every connection must pass exactly one access mode (reported as `shellAuth` by `/capabilities`):

| Mode (`shellAuth`) | When | Client must |
|---|---|---|
| `users` | the users plugin is mounted | connect with a WebSocket ticket (`?ticket=`) of a session holding `shell:execute` |
| `token` | no users plugin, `shell=CliShellOptions(token="...")` | send `Authorization: Bearer <token>` (header, or the `_header_Authorization` query parameter browser clients use) |
| `custom` | no users plugin, `shell=CliShellOptions(authorize=fn)` | pass `fn(websocket) -> bool \| Awaitable[bool]` (exceptions deny) |
| `anonymous` | only with `shell=CliShellOptions(allow_anonymous=True)` | nothing — logs a startup WARNING; never expose such a server |
| `locked` | enabled, none of the above | — every connection is closed with 4401 and an ERROR is logged at startup |

Precedence: users plugin, then `token`, then `authorize`, then `allow_anonymous`. Every *configured* check must pass: with the users plugin a configured `token` / `authorize` is required as well, and `token` + `authorize` together both apply. `allow_anonymous` is ignored as soon as another mode is configured.

**With the users plugin** (recommended) nothing else is needed:

```python
result = create_cli_server(CliServerOptions(enable_shell=True, allowed_origins=[...]))
result.mount_plugin(CliUsersBuilder().build())  # shell needs a ticket + shell:execute
```

An open shell belongs to the session that opened it: logout, session expiry, an admin revoke (kick), the user being disabled or deleted, or losing `shell:execute` closes it with 4401 `session ended` and kills its process tree (checked immediately on every session/user/group change and every `session_check_interval`, default 30 s, at most 60 s). Opens and closes are audited as `shell_opened` / `shell_closed` with `userId`, `actorId` and `metadata: { mode: "interactive"|"command", sessionId, durationMs, exitCode, reason }` — never the command text or stdin. `reason` is one of `exited`, `closed` (by the client), `session ended`, `permission removed`, `shell timed out`, `server shutdown`, `error`; `exitCode` is only present for `exited`. Re-checks never extend the session, but stdin since the last check touches it (at most once per 30 s) so an active shell user is not logged out for inactivity.

**Without the users plugin**, use a shared token (keep it out of source control):

```python
from qodalis_cli import CliServerOptions, CliShellOptions, create_cli_server

result = create_cli_server(CliServerOptions(
    enable_shell=True,
    shell=CliShellOptions(token=os.environ["QCLI_SHELL_TOKEN"]),
))
```

In the web-cli client, configure the server with `headers: { Authorization: 'Bearer <token>' }`; browsers cannot set WebSocket headers, so the client forwards it as the `_header_Authorization` query parameter, which the server accepts for the shell. Serve over TLS (`wss://`) since the token travels in the URL. Or plug in your own check:

```python
async def authorize(websocket) -> bool:
    return websocket.client.host == "127.0.0.1" and await my_sso.verify(websocket.cookies.get("sid"))

CliServerOptions(enable_shell=True, shell=CliShellOptions(authorize=authorize))
```

Outside users mode each open/close is logged at INFO with the remote address and mode (no command text).

`CliShellOptions` (times in seconds, `None` disables a timeout):

| Option | Default | Description |
|---|---|---|
| `token` | `None` | Shared secret for `token` mode (SHA-256 + constant-time compare, never logged) |
| `authorize` | `None` | `callable(websocket) -> bool \| Awaitable[bool]` for `custom` mode |
| `allow_anonymous` | `False` | Open shell without authentication (`anonymous` mode) |
| `max_sessions_per_user` | `3` | Concurrent shells per user (users mode) or per remote address |
| `max_sessions` | `20` | Concurrent shells in total |
| `idle_timeout` | `900` | Close after this long without stdin |
| `max_duration` | `None` | Close after this long regardless of activity |
| `cwd` | `None` | Shell working directory (default: the server's) |
| `shell` | `None` | Shell executable (default: `$SHELL`, then bash, then sh) |
| `env_allow` | `[]` | Extra server environment variables copied into the shell |
| `env_set` | `{}` | Variables set in the shell environment |
| `session_check_interval` | `30` | Users mode: periodic session re-check (capped at 60) |
| `trust_proxy` | `False` | Trusted proxy hops for per-address limits and logs: `False`/`0` = socket peer (header ignored), `True` = 1, `n` = n hops; the client is the `X-Forwarded-For` entry at `len - n` (rightmost for one proxy, leftmost if fewer entries) |

The shell does **not** inherit the server environment: it gets only `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `COLUMNS`, `LINES`, `SHELL`, plus `env_allow` names and `env_set` values, so server secrets (database passwords, API keys) do not leak into it.

Whenever a shell closes (client gone, timeout, session ended, shutdown, error) the server collects the shell's whole descendant tree (`ps -A -o pid=,ppid=`, Linux and macOS) **before** signalling, then sends HUP and TERM to the shell's process group and to every descendant — including `&` jobs in their own process groups and `nohup` / `setsid` children — and KILL after a short grace period. Limitation: a daemon that double-forks and is already re-parented to init (PID 1) when the shell closes is no longer in the tree and cannot be tracked; if that matters, run the shell in a container or cgroup that is torn down with it.

Refusals are sent after accepting the upgrade, so browsers can read the close code:

| Code | Reason | When |
|---|---|---|
| 4401 | `login required` / `session ended` / `shell access is not configured` | no ticket or no/non-Bearer token; session ended or `shell:execute` removed; no access mode configured |
| 4403 | `forbidden` | missing `shell:execute`, wrong token, `authorize` returned false or raised |
| 4404 | `shell disabled` | `enable_shell` is false |
| 4408 | `shell timed out` | idle timeout or max duration reached |
| 4429 | `too many shell sessions` | per-user/per-address or global limit reached |
| 1001 | `server shutdown` | the server is stopping |

A disallowed `Origin` (when `allowed_origins` is set) is closed with 4403 before any of the above.

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

The server includes a pluggable file storage system exposed at `/api/qcli/fs/*`. Enable it with `set_file_storage_provider()` and choose a storage backend.

### Filesystem API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/qcli/fs/ls?path=/` | List directory contents |
| GET | `/api/qcli/fs/cat?path=/file.txt` | Read file content |
| GET | `/api/qcli/fs/stat?path=/file.txt` | File/directory metadata |
| GET | `/api/qcli/fs/download?path=/file.txt` | Download file |
| POST | `/api/qcli/fs/upload` | Upload file (multipart) |
| POST | `/api/qcli/fs/mkdir` | Create directory |
| DELETE | `/api/qcli/fs/rm?path=/file.txt` | Delete file or directory |

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
            builder.set_file_storage_provider(InMemoryFileStorageProvider())

            # OS filesystem
            # builder.set_file_storage_provider(OsFileStorageProvider())

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

## Data Explorer

The Data Explorer plugin provides interactive access to data sources through a provider-based API. Each data source type (SQL, MongoDB, etc.) is a separate plugin implementing `IDataExplorerProvider`.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/qcli/data-explorer/sources` | List registered data sources with metadata |
| POST | `/api/qcli/data-explorer/execute` | Execute a query against a named source |

### SQL Provider

```python
from qodalis_cli_data_explorer_sql import SqlDataExplorerProvider
from qodalis_cli_server_abstractions import (
    DataExplorerProviderOptions,
    DataExplorerLanguage,
    DataExplorerOutputFormat,
    DataExplorerTemplate,
)

result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: builder.add_data_explorer_provider(
            SqlDataExplorerProvider("app.db"),
            DataExplorerProviderOptions(
                name="app-database",
                description="Application database",
                language=DataExplorerLanguage.SQL,
                default_output_format=DataExplorerOutputFormat.TABLE,
                timeout=30000,
                max_rows=1000,
                templates=[
                    DataExplorerTemplate(
                        "list_tables",
                        "SELECT name FROM sqlite_master WHERE type='table'",
                        "List all tables",
                    ),
                ],
            ),
        ),
    )
)
```

### MongoDB Provider

```python
from qodalis_cli_data_explorer_mongo import MongoDataExplorerProvider

builder.add_data_explorer_provider(
    MongoDataExplorerProvider("mongodb://localhost:27017", "myapp"),
    DataExplorerProviderOptions(
        name="mongo-primary",
        description="Primary MongoDB database",
        language=DataExplorerLanguage.JSON,
        default_output_format=DataExplorerOutputFormat.JSON,
        templates=[
            DataExplorerTemplate("show_collections", "show collections", "List all collections"),
            DataExplorerTemplate("find_users", "db.users.find({})", "Find all users"),
        ],
    ),
)
```

**Supported MongoDB operations:** `db.collection.find({...})`, `findOne`, `aggregate([...])`, `insertOne`, `insertMany`, `updateOne`, `updateMany`, `deleteOne`, `deleteMany`, `countDocuments`, `distinct`. Convenience commands: `show collections`, `show dbs`.

### Custom Provider

Implement `IDataExplorerProvider` to add your own data source:

```python
from qodalis_cli_server_abstractions import (
    IDataExplorerProvider,
    DataExplorerExecutionContext,
    DataExplorerResult,
)

class MyProvider(IDataExplorerProvider):
    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        # context.query — the user's query string
        # context.parameters — key-value parameters
        # context.options — provider options (name, language, etc.)
        return DataExplorerResult(
            success=True,
            source=context.options.name,
            language=context.options.language,
            default_output_format=context.options.default_output_format,
            execution_time=0,
            columns=["id", "name"],       # None for document-oriented results
            rows=[[1, "Alice"], [2, "Bob"]],  # dicts when columns is None
            row_count=2,
            truncated=False,
            error=None,
        )

builder.add_data_explorer_provider(
    MyProvider(),
    DataExplorerProviderOptions(name="custom", description="My custom source"),
)
```

The same provider class can be registered multiple times with different configurations (e.g., two databases with different names).

## AWS Cloud Services

The AWS plugin adds commands for managing AWS resources (S3, EC2, Lambda, CloudWatch, SNS, SQS, IAM, DynamoDB, ECS) directly from the CLI. It uses boto3 and supports the full credential chain.

```python
from qodalis_cli_aws import AwsModule

result = create_cli_server(
    CliServerOptions(configure=lambda builder: builder.add_module(AwsModule()))
)
```

### Authentication

The plugin resolves credentials in this order:

1. **CLI configure**: `aws configure set --key <KEY> --secret <SECRET> --region <REGION>`
2. **Environment variables**: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
3. **AWS profiles**: `aws configure set --profile <name>`
4. **IAM roles**: Automatic on EC2/ECS/Lambda

Verify connectivity with `aws status`.

### Available Commands

| Service | Commands |
|---------|----------|
| **configure** | `aws configure set`, `aws configure get`, `aws configure profiles` |
| **status** | `aws status` — STS GetCallerIdentity connectivity check |
| **S3** | `aws s3 ls`, `aws s3 cp`, `aws s3 rm`, `aws s3 mb`, `aws s3 rb`, `aws s3 presign` |
| **EC2** | `aws ec2 list`, `aws ec2 describe`, `aws ec2 start`, `aws ec2 stop`, `aws ec2 reboot`, `aws ec2 sg list` |
| **Lambda** | `aws lambda list`, `aws lambda invoke`, `aws lambda logs` |
| **CloudWatch** | `aws cloudwatch alarms`, `aws cloudwatch logs`, `aws cloudwatch metrics` |
| **SNS** | `aws sns topics`, `aws sns publish`, `aws sns subscriptions` |
| **SQS** | `aws sqs list`, `aws sqs send`, `aws sqs receive`, `aws sqs purge` |
| **IAM** | `aws iam users`, `aws iam roles`, `aws iam policies` |
| **DynamoDB** | `aws dynamodb tables`, `aws dynamodb describe`, `aws dynamodb scan`, `aws dynamodb query` |
| **ECS** | `aws ecs clusters`, `aws ecs services`, `aws ecs tasks` |

All commands support `--region` (`-r`) for region override and `--output` (`-o`) for format selection (`table`, `json`, `text`). Destructive commands support `--dry-run`.

See [`plugins/aws/README.md`](plugins/aws/README.md) for the full command reference.

### Users Plugin

The users plugin (`qodalis-cli-server-plugin-users`) adds endpoints under `/api/v1/qcli` for authentication, users, groups with permissions, sessions and an audit log, with swappable storage (in-memory by default). It is the server side of the `@qodalis/cli-users` browser plugin's *server mode*.

```python
from qodalis_cli_users import CliUsersBuilder

users_plugin = (
    CliUsersBuilder()
    .set_default_admin_password("admin")
    .build(broadcast_fn=result.event_socket_manager.broadcast_message)
)
result.mount_plugin(users_plugin)  # protects every route and socket of the server
# In the app lifespan: await users_plugin.startup() / await users_plugin.shutdown()
```

Once mounted, every CLI server route and WebSocket requires a users session (login, version discovery and health stay public); WebSockets authenticate with single-use tickets from `POST /api/v1/qcli/sessions/ws-ticket`.

See [`plugins/users/README.md`](plugins/users/README.md) for options, endpoints, custom storage and external auth adapters.

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
  data-explorer/                      # Base data exploration framework
  data-explorer-sql/                  # SQLite data explorer provider
  data-explorer-mongo/                # MongoDB data explorer provider
  data-explorer-postgres/             # PostgreSQL data explorer provider
  data-explorer-mysql/                # MySQL data explorer provider
  data-explorer-mssql/                # MS SQL Server data explorer provider
  data-explorer-redis/                # Redis data explorer provider
  data-explorer-elasticsearch/        # Elasticsearch data explorer provider
  users/                              # Users plugin (auth, sessions, groups, audit)
  weather/                            # Weather module (example plugin)
  admin/                              # Admin dashboard and monitoring
  jobs/                               # Job scheduling and management
  aws/                                # AWS cloud resource management
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
    cli_log_socket_manager.py         # WebSocket log streaming
    cli_shell_session_manager.py      # Interactive PTY shell sessions (access, limits, env)
  controllers/
    cli_controller.py                 # REST API (/api/v1/qcli)
    cli_version_controller.py         # Version discovery (/api/qcli/versions)
    filesystem_controller.py          # Filesystem API (/api/qcli/fs)
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
