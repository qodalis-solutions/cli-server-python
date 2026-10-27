# qodalis-cli-server-abstractions

Zero-dependency base types for writing [Qodalis CLI](https://cli.qodalis.com/docs) server plugins in Python.

[![PyPI](https://img.shields.io/pypi/v/qodalis-cli-server-abstractions)](https://pypi.org/project/qodalis-cli-server-abstractions/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/qodalis-solutions/cli-server-python/blob/main/LICENSE)

Depend on this package from a plugin (command processors, modules, jobs, data explorer providers) without pulling in FastAPI, uvicorn or websockets. The server, [`qodalis-cli-server`](https://pypi.org/project/qodalis-cli-server/), re-exports the same types.

## Install

```bash
pip install qodalis-cli-server-abstractions
```

## Example

```python
from qodalis_cli_server_abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
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
        return [CliCommandParameterDescriptor("name", "Name to greet", aliases=["-n"])]

    async def handle_async(self, command: CliProcessCommand, cancellation_event=None) -> str:
        return f"Hello, {command.args.get('name', 'World')}!"


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
    def processors(self):
        return [GreetCommandProcessor()]
```

Register it on the server with `builder.add_module(GreetingsModule())`; see the [server README](https://github.com/qodalis-solutions/cli-server-python#readme).

## Contents

| Area | Types |
|---|---|
| Processors | `ICliCommandProcessor`, `CliCommandProcessor`, `ICliStreamCommandProcessor`, `is_stream_capable`, `ICliProcessorFilter` |
| Input | `CliProcessCommand`, `ICliCommandParameterDescriptor`, `CliCommandParameterDescriptor`, `split_shell_words`, `join_shell_words`, `get_positionals`, `get_positional_text` |
| Modules and metadata | `ICliModule`, `CliModule`, `ICliCommandAuthor`, `CliCommandAuthor`, `DEFAULT_LIBRARY_AUTHOR` |
| Jobs | `ICliJob`, `ICliJobExecutionContext`, `ICliJobLogger`, `CliJobOptions`, `ICliJobStorageProvider`, `JobExecution`, `JobState`, `JobLogEntry` |
| Data explorer | `IDataExplorerProvider`, `DataExplorerProviderOptions`, `DataExplorerExecutionContext`, `DataExplorerResult`, `DataExplorerLanguage`, `DataExplorerOutputFormat`, `DataExplorerTemplate`, `DataExplorerParameterDescriptor`, schema types |

### Processor properties

| Property | Type | Default | Description |
|---|---|---|---|
| `command` | `str` | abstract | Name users type |
| `description` | `str` | abstract | Help text |
| `handle_async(command, cancellation_event=None)` | method | abstract | Runs the command, returns text |
| `handle_structured_async(command, cancellation_event=None)` | method | falls back to `handle_async` | Return a structured server response |
| `parameters` | `list \| None` | `None` | Declared parameters (help, autocompletion, MCP schemas) |
| `processors` | `list \| None` | `None` | Sub-commands |
| `allow_unlisted_commands` | `bool \| None` | `None` | Accept sub-commands not in `processors` |
| `value_required` | `bool \| None` | `None` | Whether a positional value is required |
| `required_permissions` | `list[str] \| None` | `None` | Enforced when the users plugin protects the server |
| `version` | `str` | `"1.0.0"` | Processor version |
| `author` | `ICliCommandAuthor` | `DEFAULT_LIBRARY_AUTHOR` | Author metadata |

## License

MIT
