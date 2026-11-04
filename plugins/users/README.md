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
    await users_plugin.startup()   # seed built-ins + start the session sweep (and adapter socket re-checks)
    try:
        async with original_lifespan(app) as state:
            yield state
    finally:
        await users_plugin.shutdown()

result.app.router.lifespan_context = lifespan
```

`startup()` seeds the built-in groups `admin` (`["*"]`) and `users` (`["commands:execute"]`) and, when the user store is empty and no external auth adapter is set, a `root` user (`root@localhost`, group `admin`). Without `set_default_admin_password(...)` a random 16-character password is generated, logged **once** (never to log sockets or the admin log buffer) and root must change it at first login; a well-known explicit password (`root`, `admin`, `password`) logs a warning. Requests that arrive before `startup()` also trigger seeding.

## Builder

| Method | Default | Description |
|--------|---------|-------------|
| `set_user_store` / `set_session_store` / `set_credential_store` / `set_group_store` / `set_audit_store` / `set_api_token_store` | in-memory | Storage implementations |
| `set_allow_non_expiring_tokens(bool)` | `False` | Allow API tokens that never expire (`expiresInDays: null`) |
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
| `set_read_only_users(bool=True)` | `False` | The host owns the users: user writes answer `501` (see [Read-only stores](#read-only-stores)) |
| `set_read_only_groups(bool=True)` | `False` | The host owns the groups: group writes answer `501` |
| `set_auth_adapter(adapter)` | `None` | Delegate authentication to the host app (a sync or async callable; see below) |
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

Groups carry `permissions` (`resource:action` strings, `*` wildcards such as `users:*` or `*`, case-insensitive). A user's effective permissions are the union of its groups' permissions. Built-in permissions: `users:read|create|update|delete`, `groups:read|create|update|delete`, `sessions:read|revoke`, `audit:read|clear`, `tokens:read|revoke`, plus the server permissions below.

A caller can never grant or exercise more than it holds: group permissions it grants, groups it adds users to, and users it acts on (update, password reset, delete, revoke sessions) must be covered by its own permissions (403).

### Server protection

When mounted on a CLI server, every route and socket requires a users session except `POST /api/v1/qcli/sessions`, version discovery (`/api/qcli/versions`, `/api/v1/qcli/version`, `/api/v1/qcli/capabilities`) and health endpoints:

| Surface | Permission |
|---|---|
| `GET /commands` | authenticated |
| `POST /execute`, `/execute/stream` | `commands:execute` + the processor's `required_permissions` |
| `POST /api/qcli/mcp` (MCP endpoint) | `commands:execute`; each tool also needs its own permissions |
| fs `ls`, `cat`, `stat`, `download` | `files:read` |
| fs `upload`, `mkdir`, `rm` | `files:write` |
| data explorer | `data:query` |
| jobs read / manage | `jobs:read` / `jobs:manage` |
| events WebSocket | authenticated |
| logs WebSocket | `logs:read` |
| shell WebSocket (opt-in `enable_shell`) | `shell:execute` |
| admin dashboard (admin plugin with `set_users_plugin`) | `admin:access` |

WebSockets authenticate with a single-use ticket: `POST /sessions/ws-ticket` → `201 { ticket, expiresIn: 30 }`, then connect with `?ticket=<ticket>`. Missing/invalid tickets close with 4401, missing permissions with 4403. Bearer tokens are not accepted in query strings.

Events and logs sockets stay bound to the session behind their ticket: on logout, revoke (kick), the user being disabled or deleted, or session expiry (detected on every session/user/group change and by the background sweep) they are closed with 4401 `session ended`. Revocations (kick, disable, delete, password reset) first send `session-kicked` to the user's event sockets. Sockets opened through an external auth adapter are re-checked through the adapter every `session_check_interval` (see [External auth adapter](#external-auth-adapter)).

Remote shell: when mounted, the plugin owns shell access (`shellAuth: "users"`). A shell is bound to the session that opened it and is closed with 4401 `session ended` after logout, expiry, revoke, user disable/delete or loss of `shell:execute` (re-checked immediately on every change and every 30 s by default, at most 60 s). Opens and closes are audited as `shell_opened` / `shell_closed` (`metadata: { mode, sessionId, durationMs, exitCode, reason }`; `reason` is `permission removed` when `shell:execute` was lost; never the command). See the main README, *Remote Shell Security*.

While `mustChangePassword` is true a session may only use `GET`/`DELETE /sessions/current` and `PUT /users/{self}/password`; everything else returns `403 password change required`.

## API Endpoints

All routes are mounted under `/api/v1/qcli`; collections accept paths with and without a trailing slash. Authenticated routes expect `Authorization: Bearer <token>`. Errors are always `{ "error": "<message>" }` (validation errors are `400`).

| Endpoint | Requirement | Description |
|----------|-------------|-------------|
| `POST /sessions` | public | Login `{ username, password }` (name or email) → `201 { token, session }` |
| `POST /sessions/ws-ticket` | authenticated | Single-use WebSocket ticket `{ ticket, expiresIn }` |
| `GET /sessions/current` | authenticated | Current session `{ id, user, permissions, loginTime, lastActivity, expiresAt, usersReadOnly, groupsReadOnly }` |
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
| `GET /tokens` | authenticated | Own API tokens (views) |
| `GET /tokens?all=true` | `tokens:read` | Every user's API tokens (`all` must be `true`/`false`, else 400) |
| `POST /tokens` | authenticated, **session only** | `{ name, expiresInDays?, permissions? }` → `201 { token, apiToken }` |
| `DELETE /tokens/{id}` | owner or `tokens:revoke` | Revoke (204); unknown or not yours → 404 |

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

## API tokens

Long-lived, revocable bearer tokens for MCP clients, CI and scripts (sessions expire and need a password sign-in). A token is `qcli_pat_` + 32 random bytes (base64url, no padding); it is shown **once** at creation and only its SHA-256 hash is stored (`IApiTokenStore`, in-memory by default — replace it with `set_api_token_store`).

```bash
# create (needs a session: an API token gets 403, an external-adapter identity 501)
curl -X POST http://localhost:8048/api/v1/qcli/tokens \
  -H "Authorization: Bearer <session token>" -H "Content-Type: application/json" \
  -d '{"name": "claude", "expiresInDays": 30, "permissions": ["commands:execute", "files:read"]}'
# → 201 { "token": "qcli_pat_…", "apiToken": { "id", "name", "userId", "userName", "createdAt", "expiresAt", "lastUsedAt", "permissions" } }
```

- `name`: 1-64 characters of `[A-Za-z0-9._ -]`, unique per user (case-insensitive; duplicate → `409`). At most 20 non-expired tokens per user (`400`; expired tokens stay listed until revoked).
- `expiresInDays`: integer 1-365, default 90; `null` (never expires) only with `set_allow_non_expiring_tokens(True)`, else `400`.
- `permissions` (optional scope): every entry must be a valid permission you hold, else `403 forbidden: cannot grant <perm>` (body field errors are `400` first). A scoped token gets the user's **live** permissions intersected with the scope; `[]` is an empty scope; an unscoped token (`permissions` omitted or `null`; omitted in the view) gets all of them.
- Views never contain the token or its hash; `lastUsedAt` is `null` until first use and updated at most once a minute.

`Authorization: Bearer qcli_pat_…` is accepted everywhere a session token is (HTTP routes, `POST /sessions/ws-ticket`, the MCP endpoint). An unknown, revoked or expired token, or one whose user is disabled or deleted, gets `401`; `mustChangePassword` applies as for sessions. With a token, `GET`/`DELETE /sessions/current` answer `404 not a session` and password changes `403 forbidden: API tokens cannot change passwords`. Sockets opened with a token's ticket are closed with 4401 `session ended` when the token is revoked or expires or the user is disabled.

Deleting a user deletes their tokens; disabling a user makes their tokens fail (they work again after re-enabling). Creation and revocation are audited as `token_created` / `token_revoked` (details: token name and id).

Use a token with the server's MCP endpoint — see the main README, [*Using the MCP endpoint from an MCP client*](../../README.md#using-the-mcp-endpoint-from-an-mcp-client):

```bash
claude mcp add --transport http qcli http://localhost:8048/api/qcli/mcp \
  --header "Authorization: Bearer <token>"
```

## Session events

Every revoked session is broadcast as:

```json
{ "type": "session-kicked", "data": { "sessionId": "...", "userId": "...", "reason": "revoked" } }
```

Reasons: `revoked`, `user_deleted`, `user_disabled`, `password_changed`. A `@qodalis/cli-users` client in server mode logs itself out when its own session is kicked.

## Custom storage

Implement any of `IUserStore`, `ISessionStore`, `ICredentialStore`, `IAuditStore`, `IGroupStore` or `IApiTokenStore` (see `qodalis_cli_users.stores.interfaces`) and pass the instance to the builder:

```python
users_plugin = CliUsersBuilder().set_user_store(PostgresUserStore(pool)).build()
```

## External auth adapter

If the host application already authenticates requests, call `set_auth_adapter(adapter)`. The adapter (sync or async) receives the request and returns an `ExternalAuthResult` (user + admin flag + optional permissions) or `None`. Login, logout, password change and `verify-password` then answer `501`; no `root` user is seeded (the built-in groups still are), and adapter sessions are not stored or listed. Permissions come from the groups the adapter returns (`is_admin` adds `admin`) plus the result's `permissions`; group names missing from the group store grant nothing (and break nothing).

The adapter may return the result directly or an awaitable of it, so it can load the user, their roles or their disabled state from a database. A plain function, an `async def` function and an object with a sync or async `__call__` are all accepted; the plugin awaits the result for every request, ticket issuance and socket re-check:

```python
async def host_auth(request):
    current = await user_access.current(request)   # e.g. a database lookup
    if current is None:
        return None
    return ExternalAuthResult(user=to_cli_user(current), is_admin=current.is_admin)

users_plugin = CliUsersBuilder().set_auth_adapter(host_auth).build()
```

Hosts with their own roles can grant permissions directly instead of keeping a group per permission:

```python
return ExternalAuthResult(
    user=to_cli_user(account),                       # groups may be empty
    permissions=["commands:execute", "logs:read"],   # added to the groups' permissions
)
```

**Shells opened by adapter identities** have no stored session to re-check, so at every shell re-check (right after the shell opens, on every change event and every `session_check_interval`) the adapter is **run again on a snapshot of the request it accepted**: the `POST /sessions/ws-ticket` request the shell's ticket came from (request line, headers and cookies, client address and app; no body and no state set by middleware). The adapter must therefore be able to authenticate that request again on its own. `None`, an exception, a different user or a disabled user closes the shell with 4401 `session ended` (audit reason `session ended`); a user without `shell:execute` closes it too (audit reason `permission removed`).

**Events and logs sockets opened by adapter identities** are re-checked the same way: every `session_check_interval` (the server's `CliShellOptions.session_check_interval`, default 30 s, capped at 60 s; it applies whether or not the shell is enabled) the adapter runs again on the snapshot of the ticket request. `None`, a different user, a disabled user, an exception, or a user who no longer holds the socket's permission (`logs:read` for the logs socket; the events socket only needs authentication) closes the socket with 4401 `session ended`. The checks run from `startup()` (call it from the app's lifespan, see [Quick Start](#quick-start)), one at a time: a slow adapter delays the next check rather than stacking them. `users_plugin.recheck_external_sockets()` runs one on demand.

A user the adapter returns with `disabled=True` is refused (401 `account disabled`) on requests, ticket issuance and re-checks.

## Read-only stores

When the host application owns the users or groups (a custom `IUserStore`/`IGroupStore` over its own accounts and roles), mark them read-only instead of raising from the store's write methods:

```python
users_plugin = (
    CliUsersBuilder()
    .set_auth_adapter(host_auth)
    .set_user_store(HostUserStore())   # only the read methods need real implementations
    .set_read_only_users()
    .set_read_only_groups()
    .build()
)
```

- **`set_read_only_users()`**: `POST /users`, `PUT /users/{idOrName}` (self updates too), `DELETE /users/{idOrName}`, `PUT /users/{idOrName}/password` and `POST`/`DELETE /groups/{name}/members/{idOrName}` answer `501 { "error": "Users are managed by the host application" }` without calling any store (before authentication). No `root` user is seeded, and deleting a group leaves the users' memberships alone.
- **`set_read_only_groups()`**: `POST /groups`, `PUT /groups/{name}` and `DELETE /groups/{name}` answer `501 { "error": "Groups are managed by the host application" }` without calling the store, and the built-in `admin` and `users` groups are not seeded (the host's store provides the groups it needs).
- Reads keep working. `GET /sessions/current` and the login response (`session`) carry `usersReadOnly` and `groupsReadOnly` (always present, `false` by default) so clients can hide `adduser`, `groupadd` and the like.

## Client

Point the browser plugin at this server:

```typescript
import { usersModule } from '@qodalis/cli-users';

engine.registerModule(usersModule.configure({ server: 'python' }));
// options.servers must contain { name: 'python', url: 'http://localhost:8048' }
```

## License

MIT
