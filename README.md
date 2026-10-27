# Qodalis CLI Server (Python)

A FastAPI server that runs commands, files, data queries and shells for the Qodalis web terminal.

[![PyPI](https://img.shields.io/pypi/v/qodalis-cli-server?label=qodalis-cli-server)](https://pypi.org/project/qodalis-cli-server/)
[![PyPI abstractions](https://img.shields.io/pypi/v/qodalis-cli-server-abstractions?label=abstractions)](https://pypi.org/project/qodalis-cli-server-abstractions/)
[![Build & Test](https://github.com/qodalis-solutions/cli-server-python/actions/workflows/build.yml/badge.svg?branch=develop)](https://github.com/qodalis-solutions/cli-server-python/actions/workflows/build.yml)
[![Python](https://img.shields.io/pypi/pyversions/qodalis-cli-server)](https://pypi.org/project/qodalis-cli-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Overview

`qodalis-cli-server` is the Python backend of the [Qodalis CLI](https://cli.qodalis.com/docs). You write command processors in Python, register them on a FastAPI app, and the browser terminal discovers and runs them over REST, Server-Sent Events and WebSockets. Plugins add authentication, a file system, data explorers, scheduled jobs, an admin dashboard and an MCP endpoint for AI clients.

| Project | Role |
|---|---|
| [web-cli](https://github.com/qodalis-solutions/web-cli) | Browser terminal (Angular, React, Vue, vanilla) that connects to this server |
| **cli-server-python** | This server (FastAPI) |
| [cli-server-node](https://github.com/qodalis-solutions/cli-server-node) / [cli-server-dotnet](https://github.com/qodalis-solutions/cli-server-dotnet) | The same API in Node.js and .NET |
| [cli-server-dashboard](https://github.com/qodalis-solutions/cli-server-dashboard) | Admin dashboard SPA served by the admin plugin |
| [mcp-cli](https://github.com/qodalis-solutions/mcp-cli) | `@qodalis/mcp-cli`, a stdio bridge from Claude Code to this server's MCP endpoint |

## Features

- **Command processors** with typed parameters, sub-commands, modules and permission requirements
- **Streaming output** over SSE (`/execute/stream`) for processors implementing `ICliStreamCommandProcessor`
- **WebSockets** for server events, live logs and an opt-in PTY shell with access control and process cleanup
- **File system API** with in-memory, OS, JSON, SQLite and S3 storage providers
- **Data explorer** providers for SQLite, PostgreSQL, MySQL, SQL Server, MongoDB, Redis and Elasticsearch
- **Users plugin**: sign-in, groups and permissions, sessions, audit log, API tokens; protects every route and socket
- **MCP endpoint** exposing commands, files, data and jobs as tools for AI clients
- **Jobs**, **admin dashboard**, **AWS** and **weather** (example) plugins

## Installation

```bash
pip install qodalis-cli-server
```

Requires Python 3.10+. Plugin authors who only need the base types (`CliCommandProcessor`, `CliProcessCommand`, `CliModule`, ...) can depend on the zero-dependency [`qodalis-cli-server-abstractions`](https://pypi.org/project/qodalis-cli-server-abstractions/) package instead.

## Quick Start

```python
import uvicorn

from qodalis_cli import (
    CliCommandProcessor,
    CliProcessCommand,
    CliServerOptions,
    create_cli_server,
    install_log_redaction,
)


class GreetProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "greet"

    @property
    def description(self) -> str:
        return "Says hello"

    async def handle_async(self, command: CliProcessCommand, cancellation_event=None) -> str:
        return f"Hello, {command.value or 'World'}!"


result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: builder.add_processor(GreetProcessor()),
        allowed_origins=["http://localhost:4200"],  # your web-cli app
    )
)

install_log_redaction()  # keep tickets, tokens and shell commands out of uvicorn logs
uvicorn.run(result.app, host="0.0.0.0", port=8048)
```

Point the web terminal at `http://localhost:8048` and run `greet Ada`.

uvicorn logs request paths with their query strings, which can contain WebSocket tickets (`ticket=`), tokens (`token=`, `access_token=`, `_header_Authorization=`) and shell commands (`cmd=`). `install_log_redaction()` adds a `RedactingLogFilter` to the `uvicorn`, `uvicorn.access` and `uvicorn.error` loggers; the filter class is exported for your own handlers.

### Standalone server

The package installs a `qodalis-cli-server` command that serves the built-in processors:

```bash
qodalis-cli-server                         # http://0.0.0.0:8048
PORT=9000 HOST=127.0.0.1 qodalis-cli-server
```

## Writing Commands

### Processors

Extend `CliCommandProcessor` and implement `command`, `description` and `handle_async`. Optional properties:

| Property | Type | Default | Description |
|---|---|---|---|
| `parameters` | `list[ICliCommandParameterDescriptor] \| None` | `None` | Declared parameters (used for help, autocompletion and MCP tool schemas) |
| `processors` | `list[ICliCommandProcessor] \| None` | `None` | Sub-commands |
| `allow_unlisted_commands` | `bool \| None` | `None` | Accept sub-commands not in `processors` |
| `value_required` | `bool \| None` | `None` | `True`: a positional value is required; `False`: optional |
| `required_permissions` | `list[str] \| None` | `None` | Permissions enforced when the users plugin protects the server |
| `version` | `str` | `"1.0.0"` | Processor version |
| `author` | `ICliCommandAuthor` | library author | Author metadata |

Override `handle_structured_async` to return a `CliServerResponse` (tables, lists, JSON, key-value outputs) instead of plain text; `CliResponseBuilder` helps build one. Implement `ICliStreamCommandProcessor.handle_stream_async(command, emit, cancellation_event)` to emit output chunks over `/execute/stream`.

Every processor receives a `CliProcessCommand`:

| Field | Type | Description |
|---|---|---|
| `command` | `str` | Command name (`"time"`) |
| `value` | `str \| None` | Positional input (`"hello"` in `echo hello`) |
| `args` | `dict[str, Any]` | Named parameters (`--format "%H:%M"`) |
| `chain_commands` | `list[str]` | Sub-command chain (`["add"]` in `math add`) |
| `positionals` | `list[str] \| None` | Positional words, when the client sends them |
| `raw_command` | `str` | Original input |
| `data` | `Any` | Arbitrary payload from the client |

### Parameters

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

    async def handle_async(self, command: CliProcessCommand, cancellation_event=None) -> str:
        from datetime import datetime, timezone

        use_utc = "utc" in command.args
        fmt = command.args.get("format", "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc) if use_utc else datetime.now()
        label = "UTC" if use_utc else "Local"
        return f"{label}: {now.strftime(fmt)}"
```

Parameter types: `"string"`, `"number"`, `"boolean"`.

### Sub-commands

Return child processors from `processors` to build hierarchies such as `math add --a 5 --b 3`; `chain_commands` tells the handler which one ran. See [`demo/processors`](demo/processors) for complete examples (`math`, `time`, `hello`, `status`, `echo`).

### Modules

A module bundles processors under a name and version. Extend `CliModule` and register it with `builder.add_module(...)`, which registers each of its processors:

```python
from qodalis_cli import CliModule, ICliCommandProcessor


class GreetingsModule(CliModule):
    @property
    def name(self) -> str:
        return "greetings"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Greeting commands"

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [GreetProcessor()]


create_cli_server(CliServerOptions(configure=lambda b: b.add_module(GreetingsModule())))
```

[`plugins/weather`](plugins/weather) is a complete module (`weather current`, `weather forecast`).

### Built-in processors

Used by the standalone server; import them from `qodalis_cli` to add them to your own.

| Command | Class | Description |
|---|---|---|
| `echo` | `CliEchoCommandProcessor` | Echoes input text |
| `status` | `CliStatusCommandProcessor` | Server status (uptime, OS) |
| `system` | `CliSystemCommandProcessor` | System information (hostname, CPU, memory) |
| `http` | `CliHttpCommandProcessor` | HTTP requests (`get`, `post`), streamed |
| `hash` | `CliHashCommandProcessor` | Hash text (`--algorithm`, default `sha256`) |
| `base64` | `CliBase64CommandProcessor` | Base64 encode / decode |

## Configuration

`create_cli_server(options)` takes a `CliServerOptions`:

| Option | Default | Description |
|---|---|---|
| `configure` | `None` | `Callable[[CliBuilder], None]` that registers processors, modules and providers |
| `allowed_origins` | `None` | Origins accepted for CORS and WebSocket upgrades (`*` globs allowed, e.g. `http://127.0.0.1:*`); `None` disables the Origin check |
| `cors` | `None` | `True`/`False` forces CORS on/off; `None` enables it only when origins are configured |
| `cors_origins` | `None` | CORS origins, when they differ from `allowed_origins` |
| `base_path` | `"/api/qcli"` | Additional mount point of the command routes (always also at `/api/v1/qcli`) |
| `enable_shell` | `False` | Enable the PTY shell WebSocket, see [Remote Shell Security](#remote-shell-security) |
| `shell` | `CliShellOptions()` | Shell access, limits and environment |
| `mcp` | `None` | `True`, a `CliMcpOptions` or a `dict` enables the [MCP endpoint](#mcp-endpoint) |

CORS never combines `*` with credentials. With `allowed_origins` set, a WebSocket upgrade from another origin is closed with 4403 (a missing `Origin`, as sent by non-browser clients, is accepted; protected sockets still need a ticket). Same-origin requests, such as the bundled admin dashboard, always pass Origin and CSRF checks (`X-Forwarded-Proto`/`X-Forwarded-Host` are honoured only with `trust_proxy`).

`CliBuilder` methods (all chainable):

| Method | Description |
|---|---|
| `add_processor(processor)` | Register a command processor |
| `add_module(module)` | Register every processor of a module |
| `set_file_storage_provider(provider)` | Enable the file system API with a storage provider |
| `add_filesystem(FileSystemOptions(allowed_paths=[...], max_upload_bytes=...))` | Enable the file system API on whitelisted OS paths |
| `add_data_explorer_provider(provider, options)` | Register a data explorer source |

`create_cli_server` returns a `CliServerResult` with `app` (FastAPI), `registry`, `builder`, `executor`, `event_socket_manager`, `log_socket_manager`, `shell_session_manager`, `mcp` (the MCP tool registry), `mount_plugin(plugin)` for router-based plugins (users, jobs, admin) and `set_shell_access_provider(...)` used by auth plugins.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/qcli/versions` | Version discovery (supported and preferred version) |
| GET | `/api/v1/qcli/version` | Server version |
| GET | `/api/v1/qcli/capabilities` | OS, shell, streaming, `shellAuth` |
| GET | `/api/v1/qcli/commands` | Registered processors with their parameters |
| POST | `/api/v1/qcli/execute` | Execute a command |
| POST | `/api/v1/qcli/execute/stream` | Execute a command, output as Server-Sent Events |
| GET/POST/DELETE | `/api/qcli/fs/*` | File system: `ls`, `cat`, `stat`, `download`, `upload`, `mkdir`, `rm` (when enabled) |
| GET/POST | `/api/qcli/data-explorer/*` | `sources`, `schema`, `execute` (when a provider is registered) |
| POST | `/api/qcli/mcp` | MCP endpoint (opt-in) |
| WS | `/ws/v1/qcli/events` | Server-push events |
| WS | `/ws/v1/qcli/logs` | Server log stream (`?level=` filter) |
| WS | `/ws/v1/qcli/shell` | PTY shell (`?cols=&rows=&cmd=`), opt-in |

Every WebSocket is also served without the version (`/ws/qcli/events`, ...). Plugins add their own routes, e.g. the users plugin's `/api/v1/qcli/sessions`, `/users`, `/groups` and `/tokens`.

## MCP Endpoint

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
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"qodalis-cli-server","version":"1.0.0b8"},"instructions":"…"}}
{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"server_info","description":"…","inputSchema":{…},"annotations":{"readOnlyHint":true}}, …]}}
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"Hello, MCP!"}],"isError":false}}
```

#### 5. Troubleshooting

| Symptom | Cause |
|---|---|
| `401 {"error": …}` | Missing `Authorization` header, or the token is unknown, expired or revoked (or its user is disabled/deleted). Create a new token. Without the users plugin: the static `token` does not match. |
| `403 {"error": "forbidden: origin not allowed"}` | The request has an `Origin` header that is not in the server's `allowed_origins` and is not the server's own origin (browser-based clients). Add the origin to `allowed_origins`. |
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

## File Storage

Enable the file system API (`/api/qcli/fs/*`) with a storage provider:

```python
from qodalis_cli import InMemoryFileStorageProvider, OsFileStorageProvider, OsProviderOptions
from qodalis_cli_filesystem_json import JsonFileStorageProvider, JsonFileProviderOptions
from qodalis_cli_filesystem_sqlite import SqliteFileStorageProvider, SqliteProviderOptions
from qodalis_cli_filesystem_s3 import S3FileStorageProvider, S3ProviderOptions

# pick one
builder.set_file_storage_provider(InMemoryFileStorageProvider())
builder.set_file_storage_provider(OsFileStorageProvider(OsProviderOptions(allowed_paths=["/srv/files"])))
builder.set_file_storage_provider(JsonFileStorageProvider(JsonFileProviderOptions(file_path="./data/files.json")))
builder.set_file_storage_provider(SqliteFileStorageProvider(SqliteProviderOptions(db_path="./data/files.db")))
builder.set_file_storage_provider(S3FileStorageProvider(S3ProviderOptions(bucket="my-cli-files", region="us-east-1")))
```

### Upload size limit

`POST /api/qcli/fs/upload` accepts files up to `max_upload_bytes` (default 100 MiB = `104857600`). A larger upload is answered with `413` (`{"detail": "File exceeds the maximum upload size of <n> bytes"}`); the body is read in chunks and never buffered beyond the cap. Set it with `FileSystemOptions` (also when a custom storage provider is set):

```python
from qodalis_cli import FileSystemOptions

builder.add_filesystem(FileSystemOptions(allowed_paths=["/srv/files"], max_upload_bytes=500 * 1024 * 1024))
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

Register data sources with `builder.add_data_explorer_provider(provider, options)`; the web terminal queries them through `/api/qcli/data-explorer` (`sources`, `schema`, `execute`). Each source type is a separate plugin implementing `IDataExplorerProvider` (`execute_async`, optional `get_schema_async`); one provider class can be registered several times under different names.

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

## Plugins

Each plugin is a separate package under [`plugins/`](plugins) with its own README.

| Package | Import | Description |
|---|---|---|
| [`qodalis-cli-server-plugin-users`](plugins/users/README.md) | `qodalis_cli_users` | Sign-in, users, groups and permissions, sessions, audit log, API tokens; protects the whole server |
| [`qodalis-cli-server-plugin-admin`](plugins/admin/README.md) | `qodalis_cli_admin` | Admin dashboard at `/qcli/admin` and its API |
| [`qodalis-cli-server-plugin-jobs`](plugins/jobs/README.md) | `qodalis_cli_jobs` | Scheduled background jobs with history and logs |
| [`qodalis-cli-server-plugin-filesystem`](plugins/filesystem/README.md) | `qodalis_cli_filesystem` | `IFileStorageProvider` with in-memory and OS providers (installed with the server) |
| [`...-filesystem-json`](plugins/filesystem-json/README.md), [`-sqlite`](plugins/filesystem-sqlite/README.md), [`-s3`](plugins/filesystem-s3/README.md) | `qodalis_cli_filesystem_json`, `_sqlite`, `_s3` | Persistent file storage |
| [`qodalis-cli-server-plugin-data-explorer`](plugins/data-explorer/README.md) | `qodalis_cli_data_explorer` | Data explorer routes and executor |
| [`...-data-explorer-sql`](plugins/data-explorer-sql/README.md), [`-postgres`](plugins/data-explorer-postgres/README.md), [`-mysql`](plugins/data-explorer-mysql/README.md), [`-mssql`](plugins/data-explorer-mssql/README.md), [`-mongo`](plugins/data-explorer-mongo/README.md), [`-redis`](plugins/data-explorer-redis/README.md), [`-elasticsearch`](plugins/data-explorer-elasticsearch/README.md) | `qodalis_cli_data_explorer_<name>` | Data explorer providers |
| [`qodalis-cli-server-plugin-weather`](plugins/weather/README.md) | `qodalis_cli_weather` | Example module (`weather current`, `weather forecast`) |
| [AWS](plugins/aws/README.md) | `qodalis_cli_aws` | `aws` commands for S3, EC2, Lambda, CloudWatch, SNS, SQS, IAM, DynamoDB, ECS (boto3) |

Router-based plugins are built with a builder and mounted on the server; run their `startup()` / `shutdown()` from the app lifespan:

```python
from qodalis_cli_users import CliUsersBuilder

users_plugin = CliUsersBuilder().build()
result.mount_plugin(users_plugin)  # protects every route and socket of the server
# app lifespan: await users_plugin.startup() ... await users_plugin.shutdown()
```

The users plugin README shows the full lifespan wiring, server protection, permissions and [API tokens](plugins/users/README.md#api-tokens).

## Docker

```bash
docker run -p 8048:8048 ghcr.io/qodalis-solutions/cli-server-python
```

The image runs the demo server (all plugins, users plugin with `root` / `admin`). To build it from source: `docker compose up --build` (or `./docker-run.sh`).

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e packages/abstractions/ -e plugins/filesystem/ -e plugins/filesystem-json/ \
    -e plugins/filesystem-sqlite/ -e plugins/filesystem-s3/ -e plugins/weather/ \
    -e plugins/jobs/ -e plugins/users/ -e plugins/admin/
pip install -e ".[test]"
pytest                                   # the suite CI runs (tests/)
```

Run the demo server (port 8048, `root` / `admin`):

```bash
cd demo
pip install -r requirements.txt
python main.py
```

| Path | Contents |
|---|---|
| `src/qodalis_cli/` | Server: `create_cli_server`, controllers, services, MCP, shell security, built-in processors |
| `packages/abstractions/` | `qodalis-cli-server-abstractions`, zero-dependency base types |
| `plugins/` | Plugin packages |
| `demo/` | Demo server with sample processors and every plugin |
| `tests/` | pytest suite |

Branches: work on `feature/*`, open pull requests into `develop`; pushes to `main` publish to PyPI and ghcr.io.

## License

[MIT](LICENSE)
