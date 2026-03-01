# qodalis-cli-server-abstractions

Shared interfaces and base classes for building [Qodalis CLI](https://qodalis.com/) command processors in Python. **Zero framework dependencies** — install this package when writing plugins, without pulling in FastAPI, uvicorn, or websockets.

## Install

```bash
pip install qodalis-cli-server-abstractions
```

## Quick Start

Create a custom command processor by subclassing `CliCommandProcessor`:

```python
from qodalis_cli_server_abstractions import (
    CliCommandProcessor,
    CliProcessCommand,
    CliCommandParameterDescriptor,
)


class GreetCommandProcessor(CliCommandProcessor):
    @property
    def command(self) -> str:
        return "greet"

    @property
    def description(self) -> str:
        return "Greets a user by name"

    @property
    def parameters(self):
        return [
            CliCommandParameterDescriptor("--name", "Name to greet", required=True),
        ]

    async def handle_async(self, command: CliProcessCommand) -> str:
        name = command.args.get("name", "World")
        return f"Hello, {name}!"
```

Then register the processor with the server package. See [`qodalis-cli-server`](https://pypi.org/project/qodalis-cli-server/) for server setup.

## API Reference

### Abstract Base Classes

| Class | Description |
|-------|-------------|
| `ICliCommandProcessor` | Abstract base defining the processor contract: command, description, author, parameters, sub-processors, and `handle_async()` |
| `CliCommandProcessor` | Convenience subclass of `ICliCommandProcessor` with sensible defaults |

### Protocols

| Protocol | Description |
|----------|-------------|
| `ICliCommandAuthor` | Author metadata: `name` and `email` (runtime-checkable) |
| `ICliCommandParameterDescriptor` | Parameter declaration: name, type, required, aliases, default value (runtime-checkable) |

### Data Classes

| Class | Description |
|-------|-------------|
| `CliProcessCommand` | Parsed command input: `command`, `value`, `args`, `chain_commands`, `raw_command`, `data` |
| `CliCommandAuthor` | Simple implementation of `ICliCommandAuthor` |
| `CliCommandParameterDescriptor` | Simple implementation of `ICliCommandParameterDescriptor` |

### Constants

| Constant | Description |
|----------|-------------|
| `DEFAULT_LIBRARY_AUTHOR` | Default `CliCommandAuthor` instance used when no author is specified |

### `ICliCommandProcessor` Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `command` | `str` | *(abstract)* | The command name users type to invoke this processor |
| `description` | `str` | *(abstract)* | Human-readable description shown in help |
| `author` | `ICliCommandAuthor` | `DEFAULT_LIBRARY_AUTHOR` | Author metadata |
| `version` | `str` | `"1.0.0"` | Processor version |
| `api_version` | `int` | `1` | Minimum API version this processor targets |
| `allow_unlisted_commands` | `bool \| None` | `None` | Accept sub-commands not in `processors` list |
| `value_required` | `bool \| None` | `None` | Whether a positional value argument is required |
| `processors` | `list \| None` | `None` | Nested sub-command processors |
| `parameters` | `list \| None` | `None` | Declared parameters for autocompletion and validation |
| `handle_async(command)` | method | *(abstract)* | Execute the command, return result string |

## License

MIT
