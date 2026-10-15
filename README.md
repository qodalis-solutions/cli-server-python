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

uvicorn's own access and error logs contain request paths **with their query strings** — WebSocket tickets (`ticket=`), tokens (`token=`, `access_token=`), the legacy `_header_Authorization=Bearer%20…` and shell commands (`cmd=`). The standalone server (`python -m qodalis_cli.server` / `qodalis-cli-server`) and the demo redact them; when you run uvicorn yourself, install the same filter before `uvicorn.run`:

```python
from qodalis_cli import install_log_redaction

install_log_redaction()  # RedactingLogFilter on uvicorn, uvicorn.access, uvicorn.error
uvicorn.run(result.app, host="0.0.0.0", port=8048)
```

`RedactingLogFilter` (a `logging.Filter`) is exported too, e.g. to attach it to your own loggers or handlers.

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
    mcp: bool | CliMcpOptions | dict | None = None  # MCP endpoint (opt-in, see MCP endpoint)
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
    mcp: McpToolRegistry                    # MCP tool providers (add_provider)

    def mount_plugin(self, plugin: object) -> None: ...  # Mount a plugin with router/dashboard
    def set_shell_access_provider(self, provider) -> None: ...  # Called by auth plugins (users mode)
```

## MCP endpoint

The server can expose **the whole system** to AI clients such as Claude Code as [MCP](https://modelcontextprotocol.io) tools: CLI commands, the filesystem API, the data explorer, background jobs and tools contributed by plugins — each with the same permissions as a CLI user. It works with or without the users plugin (which only supplies identities and API tokens).

### Enabling

MCP is off by default. Enable it with the `mcp` server option (`True`, a `CliMcpOptions` or an equivalent `dict`):

```python
from qodalis_cli import CliMcpOptions, CliServerOptions, create_cli_server

result = create_cli_server(CliServerOptions(
    configure=configure,
    mcp=CliMcpOptions(
        tools=None,            # allow-list of tool names, trailing * allowed ("weather_*")
        exclude=["fs_delete"], # deny-list (same syntax), applied after `tools`
        token=None,            # only WITHOUT the users plugin: required static bearer token
        instructions="Qodalis CLI server tools",  # returned by `initialize`
    ),
))
```

### Route and transport

`POST /api/qcli/mcp` speaks MCP *Streamable HTTP* in its **stateless, JSON-only** form: each request carries one JSON-RPC 2.0 message and gets an `application/json` answer (no `Mcp-Session-Id`, no SSE stream). Methods: `initialize` (protocol versions `2025-11-25`, `2025-06-18`, `2025-03-26`; the client's version is echoed when supported, otherwise `2025-11-25`; a missing `protocolVersion` is `-32602`), `ping`, `tools/list`, `tools/call`; notifications get `202` with an empty body. JSON-RPC errors (`-32700` parse error, `-32600` invalid request or batch, `-32601` unknown method, `-32602` invalid params / unknown tool / invalid arguments) use HTTP 200. `GET` and `DELETE` answer `405` (`Allow: POST`). No particular `Accept` header is required.

A request with an `Origin` header must come from one of the server's `allowed_origins` or from the server's own origin, otherwise `403` (DNS-rebinding protection). The Origin check and the `405`s run before authentication.

### Authentication and permissions

- **With the users plugin protecting the server** (`set_protect_server(True)`, the default): the server protection authenticates the request (a session token or an [API token](plugins/users/README.md#api-tokens)) and requires `commands:execute`; `401`/`403` are plain `{ "error": … }` responses. Tools are filtered per caller: a tool is listed — and callable — only when the caller holds its permissions.
- **Without an auth provider** (or with `set_protect_server(False)`): tools are not filtered; when `token` is set, `Authorization: Bearer <token>` is required (constant-time compare, else `401`); otherwise the endpoint is as open as `/execute`.

### Tools

Tools are collected from **tool providers** on every request, so registrations, enabled features and the caller's permissions are always current. `tools` / `exclude` apply to every tool.

| Provider | Tools | Permissions |
|---|---|---|
| server (always) | `server_info` — version, capabilities, enabled features, command names | — |
| commands (always) | one tool per command (below) | `commands:execute` + every `required_permissions` along the command chain |
| filesystem (when `/api/qcli/fs` is enabled) | `fs_list {path}`, `fs_stat {path}`, `fs_read {path, maxBytes?=262144}` (UTF-8 text, longer files end with `[truncated: N of M bytes]`; binary files → error `binary file, N bytes`) | `files:read` |
| | `fs_write {path, content}`, `fs_mkdir {path}`, `fs_delete {path, recursive?=false}` | `files:write` |
| data explorer (when a provider is registered) | `data_sources`, `data_schema {source}`, `data_query {source, query, parameters?}` | `data:query` |
| jobs (jobs plugin mounted) | `jobs_list`, `jobs_get {id}`, `jobs_history {id, limit?=20}` | `jobs:read` |
| | `jobs_trigger`, `jobs_pause`, `jobs_resume`, `jobs_stop`, `jobs_cancel` `{id}` | `jobs:manage` |

Read-only tools carry the MCP annotation `readOnlyHint`, `fs_delete` / `jobs_stop` / `jobs_cancel` carry `destructiveHint`. Feature tools reuse the same services as the HTTP routes (file storage provider and its path whitelist, data-explorer executor and its limits, job scheduler); their errors come back as `isError: true` results. The interactive shell is never a tool.

**Command tools.** Every root processor and its sub-processors are walked; a node becomes a tool when it has no sub-processors, declares `parameters`, or takes a value (`value_required` is `True` — required — or `False` — optional). The name is the command path joined with `_` (`weather`, `weather_current`), lower-cased, characters outside `[a-z0-9_-]` replaced by `-`, at most 64 characters. The input schema has one property per parameter (`string`/`number`/`boolean`, `array` → array of strings, anything else → string; with `description`, `default` and `required`) plus `value` (the positional input) when the command takes a value, and `additionalProperties: false`. A call is validated against the schema, then runs through the same executor as `/execute`; the result is `{ content: [{ type: "text", text }], isError }` with the outputs rendered as text (tables as `a | b` rows, lists one item per line, JSON pretty-printed, key-value as `key: value`) and `isError` = `exitCode != 0`. A single JSON-object output is also returned as `structuredContent`.

Name collisions: built-in feature tools (server, filesystem, data explorer, jobs) win, then plugin/host providers in registration order, then command tools (a warning is logged for each skipped tool).

### Adding tools (provider API)

Plugins and host apps contribute tools with `result.mcp.add_provider(provider)` (available whether or not the endpoint is enabled). A provider has a `get_tools(context)` method (sync or async) returning `McpTool` definitions:

```python
from qodalis_cli import McpTool, McpToolResult
from qodalis_cli.mcp import object_schema

class DeployToolProvider:
    def get_tools(self, context):          # context.auth: caller (None without an auth provider)
        async def call(arguments, ctx):    # arguments are validated against input_schema first
            version = await deploy(arguments["version"])
            return McpToolResult(text=f"deployed {version}", structured={"version": version})

        return [McpTool(
            name="deploy_release",                         # [a-z0-9_-], max 64
            description="Deploy a release",
            input_schema=object_schema({"version": {"type": "string"}}, ["version"]),
            call=call,
            required_permissions=["deploy:run"],           # checked for list and call
            annotations={"destructiveHint": True},         # optional MCP tool annotations
        )]

result.mcp.add_provider(DeployToolProvider())
```

A plugin can register its provider from its `on_mount(server)` hook (`server.mcp.add_provider(...)`), as the jobs plugin does.

### Using the MCP endpoint from an MCP client

Step by step, against the demo server (`cd demo && python main.py`, or Docker: `http://localhost:8048`, users plugin with `root` / `admin`).

#### 1. Enable MCP on the server

```python
from qodalis_cli import CliMcpOptions, CliServerOptions, create_cli_server

result = create_cli_server(CliServerOptions(
    configure=configure,
    mcp=True,  # or CliMcpOptions(tools=..., exclude=..., token=..., instructions=...)
))
```

The endpoint is `POST http://<host>:<port>/api/qcli/mcp`.

#### 2. Get a credential

**With the users plugin** (protected server), sign in and create an [API token](plugins/users/README.md#api-tokens). Tokens are created with a session (a password sign-in), never with another token:

```bash
# 1) sign in → session token
SESSION=$(curl -s -X POST http://localhost:8048/api/v1/qcli/sessions \
  -H "Content-Type: application/json" \
  -d '{"username": "root", "password": "admin"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')

# 2) create an API token (shown once; expires in 30 days)
TOKEN=$(curl -s -X POST http://localhost:8048/api/v1/qcli/tokens \
  -H "Authorization: Bearer $SESSION" \
  -H "Content-Type: application/json" \
  -d '{"name": "mcp-client", "expiresInDays": 30}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')

echo "$TOKEN"   # qcli_pat_…
```

Add `"permissions": ["commands:execute", "files:read"]` to the token body to restrict what the client may do (every entry must be a permission you hold). From the Qodalis CLI connected to the server, `token create mcp-client` does the same.

**Without the users plugin**, set a static token with `mcp=CliMcpOptions(token="<long random secret>")` and use it as `<token>` below. With neither, the endpoint is open (as open as `/execute`) and the `Authorization` header can be left out.

#### 3. Connect Claude Code

**a) Directly over HTTP** with the API token (Claude Code speaks Streamable HTTP):

```bash
claude mcp add --transport http qcli http://localhost:8048/api/qcli/mcp \
  --header "Authorization: Bearer <token>"
```

**b) Through the [`@qodalis/mcp-cli`](https://github.com/qodalis-solutions/mcp-cli) bridge**, a local stdio MCP server configured with environment variables:

```bash
claude mcp add qcli -e QCLI_URL=http://localhost:8048 -e QCLI_TOKEN=<token> -- npx -y @qodalis/mcp-cli
```

Use `-e QCLI_USER=<name> -e QCLI_PASSWORD=<password>` instead of `QCLI_TOKEN` to sign in with a user, and `-e QCLI_READ_ONLY=true` to expose only read-only tools.

Check the connection with `claude mcp list` (or `/mcp` inside Claude Code); `qcli` should be listed as connected, with the server's tools.

#### 4. Raw requests with curl

Every request is one JSON-RPC message; the answer is plain JSON (no session id, no event stream):

```bash
# initialize
curl -s http://localhost:8048/api/qcli/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "curl", "version": "1.0"}}}'

# tools/list
curl -s http://localhost:8048/api/qcli/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}'

# tools/call
curl -s http://localhost:8048/api/qcli/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hello", "arguments": {"name": "MCP"}}}'
```

Responses (abridged):

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"qodalis-cli-server","version":"1.0.0b7"},"instructions":"…"}}
{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"server_info","description":"…","inputSchema":{…},"annotations":{"readOnlyHint":true}}, …]}}
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"Hello, MCP!"}],"isError":false}}
```

#### 5. Troubleshooting

| Symptom | Cause |
|---|---|
| `401 {"error": …}` | Missing `Authorization` header, or the token is unknown, expired or revoked (or its user is disabled/deleted). Create a new token. Without the users plugin: the static `token` does not match. |
| `403 {"error": "origin not allowed"}` | The request has an `Origin` header that is not in the server's `allowed_origins` and is not the server's own origin (browser-based clients). Add the origin to `allowed_origins`. |
| `403 {"error": "missing permission: commands:execute"}` | The user (or the token's scope) lacks `commands:execute`, required for the endpoint. |
| `403 {"error": "password change required"}` | The account must change its password first. |
| `405` | `GET`/`DELETE`: the endpoint only accepts `POST` (stateless, no SSE stream). |
| A tool is missing from `tools/list` | The caller lacks the tool's permissions (`files:read`/`files:write`, `data:query`, `jobs:read`/`jobs:manage`, a command's `required_permissions`, or the token's scope), the tool is filtered by the `tools`/`exclude` options, or its feature is not enabled (filesystem, data explorer, jobs plugin). |
| `-32602 Unknown tool: <name>` | Same as above: the tool is not visible to this caller. |
| `-32602 Invalid arguments for tool <name>: …` | The arguments do not match the tool's `inputSchema`. |
| `claude mcp list` shows `qcli` as failed, or `/mcp` lists no tools | Run the curl checks of step 4 with the same URL and token: they show the HTTP status and error. Re-add the server after creating a new token (`claude mcp remove qcli`, then `claude mcp add …`). |

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

An open shell belongs to the session that opened it: logout, session expiry, an admin revoke (kick), the user being disabled or deleted, or losing `shell:execute` closes it with 4401 `session ended` and kills its process tree (checked once right after the shell is registered — so a logout racing the connection is not missed — then immediately on every session/user/group change and every `session_check_interval`, default 30 s, at most 60 s). Opens and closes are audited as `shell_opened` / `shell_closed` with `userId`, `actorId` and `metadata: { mode: "interactive"|"command", sessionId, durationMs, exitCode, reason }` — never the command text or stdin. `reason` is one of `exited`, `closed` (by the client), `session ended`, `permission removed`, `shell timed out`, `server shutdown`, `error`; `exitCode` is only present for `exited`. Re-checks never extend the session, but stdin since the last check touches it (at most once per 30 s) so an active shell user is not logged out for inactivity.

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

Whenever a shell closes (client gone, timeout, session ended, shutdown, error) the server kills everything the shell started. Every shell gets a unique marker `QCLI_SHELL_ID=<random 128-bit hex>` in its environment, set after the `env_allow` / `env_set` filtering so it cannot be overridden, and inherited by every process the shell starts. The target set is collected **before** signalling: the shell's descendant tree, its process group, its session (Linux), and every process whose environment contains exactly that marker. On Linux all lookups use `/proc` (`/proc/<pid>/stat`, `/proc/<pid>/environ`), so no `ps` binary is needed (e.g. slim container images); on macOS `ps -A -o pid=,ppid=,pgid=` and `ps -A -E -ww -o pid=,command=` are used. Targets get HUP and TERM, then — after a short grace period and a second collection — KILL; the server's own process and group are never signalled. This covers `&` jobs in their own process groups, `nohup` children, and `setsid` / double-forked daemons already re-parented to init (PID 1). Limitations: a process escapes only by deliberately unsetting or changing `QCLI_SHELL_ID` before detaching, or by running as another user (its environment is unreadable); on macOS the environment of platform binaries (e.g. `/bin/sleep`) is hidden from `ps -E`, so re-parented platform binaries in a new session are not found there. If that matters, run the shell in a container or cgroup that is torn down with it.

Shell output is forwarded with backpressure: the next chunk is read from the PTY only after the previous one was sent, so a slow client makes the producing process block on the full PTY buffer instead of the server buffering its output in memory.

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

It also issues long-lived, revocable **API tokens** (`qcli_pat_…`) for MCP clients, CI and scripts (`/api/v1/qcli/tokens`).

See [`plugins/users/README.md`](plugins/users/README.md) for options, endpoints, API tokens, custom storage and external auth adapters.

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
