# qodalis-cli-server-plugin-users

User management plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Adds FastAPI endpoints for authentication, users, sessions, groups and an audit log, backed by swappable storage interfaces with in-memory defaults. It is the server side of the `@qodalis/cli-users` plugin's *server mode*.

## Install

```bash
pip install qodalis-cli-server-plugin-users
```

## Quick Start

```python
import asyncio
from qodalis_cli import create_cli_server
from qodalis_cli_users import build_users_plugin, UsersPluginOptions

result = create_cli_server(...)

users_plugin = asyncio.get_event_loop().run_until_complete(
    build_users_plugin(
        options=UsersPluginOptions(default_admin_password="admin"),
        # Optional: relay session events (e.g. session-kicked) over the events WebSocket
        broadcast_event=lambda msg: result.event_socket_manager.broadcast_message(msg),
    )
)

# Mounts the router at users_plugin.prefix (/api/v1/qcli)
result.mount_plugin(users_plugin)

# On shutdown
users_plugin.dispose()
```

On first start the plugin seeds a `root` admin user (email `root@localhost`, password from `default_admin_password`, default `root`) and an `admin` group, unless an external auth adapter is configured.

## Options

`build_users_plugin(options=None, broadcast_event=None)` takes a `UsersPluginOptions`:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `user_store` | `IUserStore` | `InMemoryUserStore` | User storage |
| `session_store` | `ISessionStore` | `InMemorySessionStore` | Session/token storage |
| `credential_store` | `ICredentialStore` | `InMemoryCredentialStore` | Password hash storage |
| `audit_store` | `IAuditStore` | `InMemoryAuditStore` | Audit log storage |
| `group_store` | `IGroupStore` | `InMemoryGroupStore` | Group storage |
| `session_timeout_ms` | `int` | `1800000` (30 min) | Idle timeout; expired sessions are purged every 5 minutes |
| `default_admin_password` | `str` | `"root"` | Password for the seeded `root` user |
| `auth_adapter` | `ExternalAuthAdapter` | `None` | Delegate authentication to the host app (see below) |

The returned `UsersPluginResult` exposes `prefix`, `router`, `auth_service`, `user_store`, `session_store`, `group_store`, `audit_store` and `dispose()`.

## API Endpoints

All routes are mounted under `/api/v1/qcli`. Protected routes expect `Authorization: Bearer <token>`.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/sessions` | POST | None | Login: `{ username, password }` → `201 { token, session }` |
| `/sessions/current` | DELETE | Required | Logout: end the current session (204) |
| `/users` | GET | Required | List users (`?query=&skip=&take=`) |
| `/users/{id_or_name}` | GET | Required | Get a user |
| `/users` | POST | Admin | Create a user `{ name, email, password?, groups?, data? }` |
| `/users/{user_id}` | PUT | Admin | Update a user |
| `/users/{user_id}` | DELETE | Admin | Delete a user and its sessions/credentials (204) |
| `/users/{user_id}/password` | PUT | Self or admin | `{ currentPassword?, newPassword }`; a wrong current password returns 403 |
| `/users/{user_id}/verify-password` | POST | Required | `{ password }` → `{ valid }` |
| `/sessions` | GET | Admin | List active sessions |
| `/sessions/current` | GET | Required | Current session |
| `/sessions/{session_id}` | DELETE | Admin | Kick a session (204) and broadcast `session-kicked` |
| `/groups` | GET | Required | List groups |
| `/groups/{name}` | GET | Required | Get a group |
| `/groups` | POST | Admin | Create a group `{ name, description? }` |
| `/groups/{name}` | DELETE | Admin | Delete a group (204) |
| `/groups/{name}/members/{user_id}` | PUT | Admin | Add a user to a group (204) |
| `/groups/{name}/members/{user_id}` | DELETE | Admin | Remove a user from a group (204) |
| `/audit` | GET | Admin | Audit entries (`?event=&username=&limit=`) |
| `/audit` | DELETE | Admin | Clear the audit log (204) |

The admin dashboard plugin keeps its own login at `/auth/login`; both plugins can be mounted at the same prefix.

Tokens are opaque random strings validated against the session store on every request; `lastActivity` is refreshed on each authenticated call.

## Session kick event

`DELETE /sessions/{session_id}` calls `broadcast_event` with:

```json
{ "type": "session-kicked", "data": { "sessionId": "...", "userId": "..." } }
```

When relayed over the events WebSocket, a `@qodalis/cli-users` client in server mode logs itself out when its own session id is kicked.

## Custom storage

Implement any of `IUserStore`, `ISessionStore`, `ICredentialStore`, `IAuditStore` or `IGroupStore` (see `qodalis_cli_users.stores.interfaces`) and pass the instance in `UsersPluginOptions`:

```python
users_plugin = await build_users_plugin(
    UsersPluginOptions(user_store=PostgresUserStore(pool))
)
```

## External auth adapter

If the host application already authenticates requests, pass an `auth_adapter` (`ExternalAuthAdapter`). It receives the request and returns an `ExternalAuthResult` (user + admin flag) or `None`. Login (`POST /sessions`), logout (`DELETE /sessions/current`) and `verify-password` then answer `501`, and no `root` user is seeded.

## Client

Point the browser plugin at this server:

```typescript
import { usersModule } from '@qodalis/cli-users';

engine.registerModule(usersModule.configure({ server: 'python' }));
// options.servers must contain { name: 'python', url: 'http://localhost:8048' }
```

The client then logs in through `POST /sessions`, attaches the bearer token to every request to that server, and gains the `sessions` and `session-kick` commands.

## License

MIT
