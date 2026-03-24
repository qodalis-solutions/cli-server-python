# qodalis-cli-server-plugin-admin

Admin dashboard plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Provides a JWT-authenticated REST API for server monitoring, plugin management, configuration, log streaming, and an optional single-page dashboard UI.

## Install

```bash
pip install qodalis-cli-server-plugin-admin
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_admin import CliAdminBuilder, AdminBuildDeps

result = create_cli_server(configure=lambda builder: builder)

admin_plugin = (
    CliAdminBuilder()
    .set_credentials("myuser", "mypassword")
    .set_jwt_secret("my-secret-key")
    .build(deps=AdminBuildDeps(
        registry=result.registry,
        event_socket_manager=result.event_socket_manager,
        builder=result.builder,
    ))
)

# Using create_cli_server:
result.mount_plugin(admin_plugin)

# Or on an existing FastAPI app:
result.app.include_router(admin_plugin.router, prefix=admin_plugin.prefix)
if admin_plugin.dashboard_app:
    result.app.mount(admin_plugin.dashboard_prefix, admin_plugin.dashboard_app)
```

## Configuration

Credentials can be set via the builder or environment variables:

| Environment Variable | Default | Description |
|---|---|---|
| `QCLI_ADMIN_USERNAME` | `admin` | Admin login username |
| `QCLI_ADMIN_PASSWORD` | `admin` | Admin login password |
| `QCLI_ADMIN_JWT_SECRET` | (auto) | JWT signing secret |

## REST API

All endpoints are mounted at the plugin's built-in prefix (`/api/v1/qcli`). Except for `/auth/login`, all endpoints require a valid JWT token.

| Method | Endpoint | Description |
|---|---|---|
| POST | `/auth/login` | Authenticate and receive a JWT token |
| GET | `/auth/me` | Get current authenticated user info |
| GET | `/status` | Server status, uptime, connected clients, enabled features |
| GET | `/plugins` | List registered plugin modules |
| POST | `/plugins/{id}/toggle` | Enable or disable a plugin module |
| GET | `/config` | Get server configuration sections |
| PUT | `/config` | Update mutable runtime settings |
| GET | `/logs` | Retrieve buffered log entries |
| GET | `/ws/clients` | List connected WebSocket clients |

## Dashboard

The plugin can serve an optional SPA dashboard. Install the `@qodalis/cli-server-dashboard` npm package and the plugin will auto-detect the built assets, or set a custom path via `set_dashboard_path()`.

## Dependencies

- `PyJWT>=2.0.0`
- `fastapi>=0.100.0`
- `psutil>=5.9.0`

## License

MIT
