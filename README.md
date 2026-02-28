# Qodalis CLI Server (Python)

A Python CLI server framework for the [Qodalis CLI](https://github.com/qodalis-solutions/angular-web-cli) ecosystem. Built with FastAPI.

## Installation

```bash
pip install qodalis-cli-server
```

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


class MyCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "greet"

    @property
    def description(self) -> str:
        return "Says hello"

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Hello from my server!"


result = create_cli_server(
    CliServerOptions(
        configure=lambda builder: builder.add_processor(MyCommandProcessor()),
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

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cli/version` | Server version |
| GET | `/api/cli/commands` | List available commands |
| POST | `/api/cli/execute` | Execute a command |
| WS | `/ws/cli/events` | WebSocket event channel |

## Creating Command Processors

```python
from qodalis_cli import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
)


class TimeCommandProcessor(CliCommandProcessor):
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

### Sub-commands

```python
class MathCommandProcessor(CliCommandProcessor):
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
        return [MathAddProcessor(), MathMultiplyProcessor()]

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Usage: math add|multiply --a <number> --b <number>"
```

## Docker

```bash
docker run -p 8048:8048 ghcr.io/qodalis-solutions/cli-server-python
```

The Docker image runs a demo server with sample processors (echo, status, time, hello, math).

## Demo

```bash
cd demo
pip install -r requirements.txt
python main.py
# Server starts on http://localhost:8048
```

## Project Structure

```
src/qodalis_cli/
  abstractions/     # ICliCommandProcessor, CliProcessCommand, parameter descriptors
  models/           # CliServerOutput, CliServerResponse, command descriptors
  services/         # Registry, executor, response builder, WebSocket manager
  controllers/      # FastAPI router for /api/cli routes
  extensions/       # CliBuilder fluent API
  processors/       # Built-in echo and status processors
  create_cli_server.py  # Factory function for standalone/library use
  server.py         # Standalone entry point
demo/               # Demo app with 5 sample processors
```

## License

MIT
