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
| Paging | `CliPaging` (`parameters()`, `from_command()`), `CliPageRequest` (`skip`, `take`), `DEFAULT_TAKE`, `MAX_TAKE` |
| Modules and metadata | `ICliModule`, `CliModule`, `ICliCommandAuthor`, `CliCommandAuthor`, `DEFAULT_LIBRARY_AUTHOR` |
| Jobs | `ICliJob`, `ICliJobExecutionContext`, `ICliJobLogger`, `CliJobOptions`, `ICliJobStorageProvider`, `JobExecution`, `JobState`, `JobLogEntry` |
| Config providers | `ICliConfigProvider`, `ICliConfigWriter`, `CliConfigSection`, `CliConfigEntry`, `CliConfigValidation`, `CliConfigContext`, `CliConfigCaller`, `cli_config_context`, `has_cli_permission`, `ICliConfigService`, `CliConfigError`, `CliConfigConflictError`, `CliConfigValidationError`, `CliConfigPermissions`, `CLI_CONFIG_CHANGED_EVENT`, `CLI_CONFIG_AUDIT_EVENT` |
| Config settings in code | `CliStoredConfigProvider` (changes kept in its store: the host's store for its id, else its own, option `store` or `set_store(store)`, `own_store`, else the server's default store, else memory; `store` is the one in use), `CliConfigSectionDefinition`, `CliConfigSettingDefinition`, `create_custom_config_provider` (`store` option), `ICliModule.config_providers` |
| Config stores | `ICliConfigStore` (`read`, `write`, optional `describe()` and `ready`, awaited by the server's `config.ready()`; each provider may have its own, one store may serve several), `CliConfigStoredSection`, `InMemoryConfigStore` (source `runtime`), `JsonFileConfigStore` (source `file`, mode 0600; `section="qcli"` keeps the settings in a section of the app's own JSON settings file, nested `app.qcli` or `app:qcli`, the rest of the file kept as it is), `cli_config_store_ready`; `describe()` says where they keep the settings |
| Where settings come from | `CliConfigStoreDescription` (`kind` `memory`, `file`, `database`, `redis` or `custom`), `describe_cli_config_store` (`custom` for a store without `describe()`), `describe_cli_config_store_line` (the `Default store:` line), `describe_cli_config_store_name` (a store in `config list`: `Redis (cache:6379/0, prefix qcli:config)`), `CliConfigProviderStorage`, `cli_config_store_storage`, `ICliConfigService.describe_default_store` |
| Config values | `validate_cli_config_value`, `parse_cli_config_input`, `parse_cli_config_duration`, `format_cli_config_duration`, `infer_cli_config_entry_type` |
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

## Config providers

The server's configuration is read and changed through providers (the admin dashboard's Config page, the server's
`config` command and the `config_*` MCP tools). A plugin brings its settings like this:

```python
from qodalis_cli_server_abstractions import CliModule
from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition, CliConfigSettingDefinition, CliConfigValidation, CliStoredConfigProvider,
)

settings = CliStoredConfigProvider(id="weather", title="Weather", sections=[
    CliConfigSectionDefinition(id="api", settings=[
        CliConfigSettingDefinition(key="units", type="enum", default="metric",
                                   validation=CliConfigValidation(options=["metric", "imperial"]),
                                   apply=lambda v: setattr(client, "units", v)),
        CliConfigSettingDefinition(key="apiKey", type="secret", apply=client.set_key),
        CliConfigSettingDefinition(key="cacheFor", type="duration", default="10m",
                                   validation=CliConfigValidation(min=60_000)),
    ]),
])


class WeatherModule(CliModule):
    ...

    @property
    def config_providers(self):
        return [settings]
```

Secrets are never returned by the server (only `isSet`); `None` resets a setting to its default. The JSON, the
validation rules, permissions, audit and events are described in the
[server README](https://github.com/qodalis-solutions/cli-server-python#config-providers).

## License

MIT
