# qodalis-cli-server-plugin-users

User management plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Adds FastAPI endpoints for authentication, users, groups with permissions, sessions and an audit log, backed by swappable storage interfaces with in-memory defaults. It is the server side of the `@qodalis/cli-users` plugin's *server mode* and implements the same contract as the node and .NET servers.

## Install

```bash
pip install qodalis-cli-server-plugin-users
```

## Quick Start

```python
from contextlib import asynccontextmanager
from qodalis_cli import create_cli_server
from qodalis_cli_users import CliUsersBuilder

result = create_cli_server(...)

users_plugin = (
    CliUsersBuilder()
    .set_default_admin_password("admin")
    # Relays session events (session-kicked) over the events WebSocket
    .build(broadcast_fn=result.event_socket_manager.broadcast_message)
)
result.mount_plugin(users_plugin)  # mounted at /api/v1/qcli

original_lifespan = result.app.router.lifespan_context

@asynccontextmanager
async def lifespan(app):
    await users_plugin.startup()   # seed built-ins + start the session sweep
    try:
        async with original_lifespan(app) as state:
            yield state
    finally:
        await users_plugin.shutdown()

result.app.router.lifespan_context = lifespan
```

`startup()` seeds the built-in groups `admin` (`["*"]`) and `users` (`[]`) and, when the user store is empty and no external auth adapter is set, a `root` user (`root@localhost`, group `admin`, password from `set_default_admin_password`, default `root`). Requests that arrive before `startup()` also trigger seeding.

`build_users_plugin(UsersPluginOptions(...))` is kept as a deprecated async wrapper.

## Builder

| Method | Default | Description |
|--------|---------|-------------|
| `set_user_store` / `set_session_store` / `set_credential_store` / `set_group_store` / `set_audit_store` | in-memory | Storage implementations |
| `set_default_admin_password(str)` | `"root"` | Password of the seeded `root` user |
| `set_session_timeout(ms)` | `1800000` (30 min) | Idle timeout, checked on every request |
| `set_min_password_length(n)` | `4` | Password policy |
| `set_max_login_attempts(n)` | `5` | Consecutive failures before lockout |
| `set_lockout(ms)` | `300000` (5 min) | Lockout duration |
| `set_cleanup_interval(ms)` | `300000` (5 min) | Background expired-session sweep |
| `set_auth_adapter(adapter)` | `None` | Delegate authentication to the host app |
| `build(broadcast_fn=None)` | | Returns a `CliUsersPlugin` (`prefix`, `router`, `startup()`, `shutdown()`, `auth_service`, stores) |

`broadcast_fn` receives JSON strings and may be sync or async.

## Permissions

Groups carry `permissions` (`resource:action` strings, `*` wildcards such as `users:*` or `*`, case-insensitive). A user's effective permissions are the union of its groups' permissions. Built-in permissions: `users:read|create|update|delete`, `groups:read|create|update|delete`, `sessions:read|revoke`, `audit:read|clear`.

## API Endpoints

All routes are mounted under `/api/v1/qcli`; collections accept paths with and without a trailing slash. Authenticated routes expect `Authorization: Bearer <token>`. Errors are always `{ "error": "<message>" }` (validation errors are `400`).

| Endpoint | Requirement | Description |
|----------|-------------|-------------|
| `POST /sessions` | public | Login `{ username, password }` (name or email) → `201 { token, session }` |
| `GET /sessions/current` | authenticated | Current session `{ id, user, permissions, loginTime, lastActivity, expiresAt }` |
| `DELETE /sessions/current` | authenticated | Logout (204) |
| `GET /sessions` | `sessions:read` | Active sessions (never includes tokens) |
| `DELETE /sessions/{id}` | `sessions:revoke` or own session | Revoke (204) |
| `GET /users?query=&skip=&take=` | `users:read` | List users |
| `GET /users/{idOrName}` | `users:read` or self | Get a user |
| `GET /users/{idOrName}/permissions` | `users:read` or self | `{ userId, groups, permissions }` |
| `POST /users` | `users:create` | `{ name, email, groups?, password?, disabled?, homeDir?, data? }` → 201 |
| `PUT /users/{idOrName}` | `users:update` (self: `email`, `data` only) | Update a user |
| `DELETE /users/{idOrName}` | `users:delete` | Delete (204) |
| `PUT /users/{idOrName}/password` | self (with `currentPassword`) or `users:update` | `{ password, currentPassword? }` → 204 |
| `POST /users/{idOrName}/verify-password` | self or `users:update` | `{ password }` → `{ valid }` |
| `GET /groups`, `GET /groups/{name}` | authenticated | Groups |
| `GET /groups/{name}/members` | `groups:read` or member | Members |
| `POST /groups` | `groups:create` | `{ name, description?, permissions? }` → 201 |
| `PUT /groups/{name}` | `groups:update` | `{ description?, permissions? }` |
| `DELETE /groups/{name}` | `groups:delete` | Delete (204) |
| `POST` / `DELETE /groups/{name}/members/{idOrName}` | `groups:update` | Add / remove member (204) |
| `GET /audit?limit=&event=&username=` | `audit:read` | Audit entries, newest first |
| `DELETE /audit` | `audit:clear` | Clear the audit log (204) |

The admin dashboard plugin keeps its own login at `/auth/login`; both plugins can be mounted at the same prefix.

### Rules

- Duplicate user name/email or group name → 409; unknown group references → `400 unknown group: x`.
- You cannot delete yourself; the last enabled member of `admin` cannot be deleted, disabled or removed from `admin`; built-in groups cannot be deleted; the `admin` group's permissions cannot be changed (all 409).
- Deleting a group removes it from every user. Deleting a user removes its credentials and revokes its sessions. Disabling a user revokes its sessions. A password change by the user revokes their other sessions; by someone else, all of the target's sessions.
- Sessions reference users by id; the live user (groups, disabled) is resolved on every request.
- Passwords are stored as `pbkdf2$100000$<saltB64>$<hashB64>` (PBKDF2-HMAC-SHA256). After 5 consecutive failed logins an account is locked for 5 minutes (`429`).

## Session events

Every revoked session is broadcast as:

```json
{ "type": "session-kicked", "data": { "sessionId": "...", "userId": "...", "reason": "revoked" } }
```

Reasons: `revoked`, `user_deleted`, `user_disabled`, `password_changed`. A `@qodalis/cli-users` client in server mode logs itself out when its own session is kicked.

## Custom storage

Implement any of `IUserStore`, `ISessionStore`, `ICredentialStore`, `IAuditStore` or `IGroupStore` (see `qodalis_cli_users.stores.interfaces`) and pass the instance to the builder:

```python
users_plugin = CliUsersBuilder().set_user_store(PostgresUserStore(pool)).build()
```

## External auth adapter

If the host application already authenticates requests, call `set_auth_adapter(adapter)`. The adapter (sync or async) receives the request and returns an `ExternalAuthResult` (user + admin flag) or `None`. Login, logout, password change and `verify-password` then answer `501`; no `root` user is seeded (the built-in groups still are), and adapter sessions are not stored or listed.

## Client

Point the browser plugin at this server:

```typescript
import { usersModule } from '@qodalis/cli-users';

engine.registerModule(usersModule.configure({ server: 'python' }));
// options.servers must contain { name: 'python', url: 'http://localhost:8048' }
```

## License

MIT
