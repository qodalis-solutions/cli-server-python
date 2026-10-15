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

users_plugin = CliUsersBuilder().build()
# Mounts the routes at /api/v1/qcli, protects every route and socket of the
# server and delivers user events only to that user's event sockets.
result.mount_plugin(users_plugin)

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

`startup()` seeds the built-in groups `admin` (`["*"]`) and `users` (`["commands:execute"]`) and, when the user store is empty and no external auth adapter is set, a `root` user (`root@localhost`, group `admin`). Without `set_default_admin_password(...)` a random 16-character password is generated, logged **once** (never to log sockets or the admin log buffer) and root must change it at first login; a well-known explicit password (`root`, `admin`, `password`) logs a warning. Requests that arrive before `startup()` also trigger seeding.

`build_users_plugin(UsersPluginOptions(...))` is kept as a deprecated async wrapper.

## Builder

| Method | Default | Description |
|--------|---------|-------------|
| `set_user_store` / `set_session_store` / `set_credential_store` / `set_group_store` / `set_audit_store` | in-memory | Storage implementations |
| `set_default_admin_password(str)` | generated | Password of the seeded `root` user |
| `set_session_timeout(ms)` | `1800000` (30 min) | Idle timeout, checked on every request |
| `set_max_session_lifetime(ms)` | `43200000` (12 h) | Absolute session lifetime |
| `set_min_password_length(n)` | `8` | Password policy (max length 1024) |
| `set_rate_limit(n, window_ms)` | `20`, `60000` | Per-IP limit for login, verify-password and self password change |
| `set_trust_proxy(bool \| int)` | `False` | Trusted proxy hops for the client IP: `False`/`0` = socket peer (header ignored), `True` = 1, `n` = n hops; the client is the `X-Forwarded-For` entry at `len - n` (rightmost for one proxy, leftmost if fewer entries) |
| `set_allowed_origins(list)` | server's `allowed_origins` | Origins accepted for bearer-less state changes (CSRF, adapter mode) |
| `set_protect_server(bool)` | `True` | Protect every CLI server route and socket when mounted |
| `set_max_login_attempts(n)` | `5` | Consecutive failures before lockout |
| `set_lockout(ms)` | `300000` (5 min) | Lockout duration |
| `set_cleanup_interval(ms)` | `300000` (5 min) | Background expired-session sweep |
| `set_auth_adapter(adapter)` | `None` | Delegate authentication to the host app |
| `build(broadcast_fn=None)` | | Returns a `CliUsersPlugin` (`prefix`, `router`, `startup()`, `shutdown()`, `require_auth`, `require_permission(p)`, `protect(app)`, `auth_service`, stores) |

When mounted with `result.mount_plugin(...)`, user-scoped events (`session-kicked`) go only to event sockets authenticated as that user. `broadcast_fn` (JSON strings, sync or async) is only used when the plugin is not mounted on a CLI server.

### Guards for host routes

```python
@app.get("/my/route")
async def handler(ctx = Depends(users_plugin.require_permission("deploy:run"))):
    return {"user": ctx.user.name}
```

`users_plugin.require_auth` only requires an authenticated session.

## Permissions

Groups carry `permissions` (`resource:action` strings, `*` wildcards such as `users:*` or `*`, case-insensitive). A user's effective permissions are the union of its groups' permissions. Built-in permissions: `users:read|create|update|delete`, `groups:read|create|update|delete`, `sessions:read|revoke`, `audit:read|clear`, plus the server permissions below.

A caller can never grant or exercise more than it holds: group permissions it grants, groups it adds users to, and users it acts on (update, password reset, delete, revoke sessions) must be covered by its own permissions (403).

### Server protection

When mounted on a CLI server, every route and socket requires a users session except `POST /api/v1/qcli/sessions`, version discovery (`/api/qcli/versions`, `/api/v1/qcli/version`, `/api/v1/qcli/capabilities`) and health endpoints:

| Surface | Permission |
|---|---|
| `GET /commands` | authenticated |
| `POST /execute`, `/execute/stream` | `commands:execute` + the processor's `required_permissions` |
| fs `ls`, `cat`, `stat`, `download` | `files:read` |
| fs `upload`, `mkdir`, `rm` | `files:write` |
| data explorer | `data:query` |
| jobs read / manage | `jobs:read` / `jobs:manage` |
| events WebSocket | authenticated |
| logs WebSocket | `logs:read` |
| shell WebSocket (opt-in `enable_shell`) | `shell:execute` |
| admin dashboard (admin plugin with `set_users_plugin`) | `admin:access` |

WebSockets authenticate with a single-use ticket: `POST /sessions/ws-ticket` → `201 { ticket, expiresIn: 30 }`, then connect with `?ticket=<ticket>`. Missing/invalid tickets close with 4401, missing permissions with 4403. Bearer tokens are not accepted in query strings.

Remote shell: when mounted, the plugin owns shell access (`shellAuth: "users"`). A shell is bound to the session that opened it and is closed with 4401 `session ended` after logout, expiry, revoke, user disable/delete or loss of `shell:execute` (re-checked immediately on every change and every 30 s by default, at most 60 s). Opens and closes are audited as `shell_opened` / `shell_closed` (`metadata: { mode, sessionId, durationMs, exitCode, reason }`; `reason` is `permission removed` when `shell:execute` was lost; never the command). See the main README, *Remote Shell Security*.

While `mustChangePassword` is true a session may only use `GET`/`DELETE /sessions/current` and `PUT /users/{self}/password`; everything else returns `403 password change required`.

## API Endpoints

All routes are mounted under `/api/v1/qcli`; collections accept paths with and without a trailing slash. Authenticated routes expect `Authorization: Bearer <token>`. Errors are always `{ "error": "<message>" }` (validation errors are `400`).

| Endpoint | Requirement | Description |
|----------|-------------|-------------|
| `POST /sessions` | public | Login `{ username, password }` (name or email) → `201 { token, session }` |
| `POST /sessions/ws-ticket` | authenticated | Single-use WebSocket ticket `{ ticket, expiresIn }` |
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
| `PUT /users/{idOrName}/password` | self (with `currentPassword`) or `users:update` | `{ password, currentPassword?, temporary? }` → 204; an admin reset sets `mustChangePassword` unless `temporary: false` |
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
- Passwords are stored as `pbkdf2$100000$<saltB64>$<hashB64>` (PBKDF2-HMAC-SHA256, computed off the event loop, at most 4 at once). After 5 consecutive failed attempts (logins, verify-password, self password change) an account is locked for 5 minutes (`429`); unknown names are locked the same way. Lockout is reserved before verification, so concurrent requests cannot exceed it.
- Session stores keep only `SHA-256(token)`; tokens, tickets and passwords never appear in logs or audit entries.
- Names and group names: 1–64 chars `^[A-Za-z0-9][A-Za-z0-9._@-]*$`; emails ≤ 254 chars; permissions ≤ 128 chars (≤ 100 per group); descriptions ≤ 256; `data` ≤ 16 KB; request bodies ≤ 1 MB; control characters are rejected. Names and emails share one namespace.
- In adapter (cookie) mode, state-changing requests without a bearer token need an `Origin` in the allowed origins (CSRF).

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

An open remote shell of an adapter identity has no stored session to re-check, so at every shell re-check (right after the shell opens, on every change event and every `session_check_interval`) the adapter is **called again with the shell's original WebSocket upgrade request** — only its headers, cookies and query string, without any state set by middleware. The adapter must therefore be able to authenticate that request on its own (e.g. from a session cookie the browser sends with the upgrade); a header the browser cannot attach to a WebSocket is not enough. `None` or an exception closes the shell with 4401 `session ended` (audit reason `session ended`); a different user, or one without `shell:execute`, closes it too (audit reason `permission removed` for the latter).

## Client

Point the browser plugin at this server:

```typescript
import { usersModule } from '@qodalis/cli-users';

engine.registerModule(usersModule.configure({ server: 'python' }));
// options.servers must contain { name: 'python', url: 'http://localhost:8048' }
```

## License

MIT
