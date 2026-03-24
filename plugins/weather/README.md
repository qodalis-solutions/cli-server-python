# qodalis-cli-server-plugin-weather

Weather information commands plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Provides current conditions and 3-day forecast lookups using the wttr.in API.

## Install

```bash
pip install qodalis-cli-server-plugin-weather
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_weather import WeatherModule

def configure(builder):
    builder.add_module(WeatherModule())

app = create_cli_server(configure=configure)
```

## Commands

| Command | Description |
|---|---|
| `weather [location]` | Show current weather conditions (defaults to London) |
| `weather current [location]` | Show current weather conditions |
| `weather forecast [location]` | Show a 3-day weather forecast |

## Parameters

| Parameter | Alias | Default | Description |
|---|---|---|---|
| `--location` | `-l` | `London` | City name to get weather for |

The location can also be passed as the command value (e.g., `weather Paris`).

## License

MIT
