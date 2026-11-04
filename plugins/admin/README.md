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

All endpoints are mounted at the plugin's built-in prefix (`/api/v1/qcli`). Except for `/auth/mode` and `/auth/login`, all endpoints require a valid JWT token.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/auth/mode` | Public. Which sign-in the dashboard shows: `{ "mode": "password" \| "external", "signInUrl": string \| null }` |
| POST | `/auth/login` | Authenticate and receive a JWT token (501 in external mode) |
| GET | `/auth/me` | Get current authenticated user info |
| GET | `/status` | Server status, uptime, connected clients, enabled features |
| GET | `/plugins` | List registered plugin modules |
| POST | `/plugins/{id}/toggle` | Enable or disable a plugin module |
| GET | `/config` | Get server configuration sections |
| PUT | `/config` | Update mutable runtime settings |
| GET | `/logs` | Retrieve buffered log entries |
| GET | `/ws/clients` | List connected WebSocket clients |

With the users plugin (`set_users_plugin(...)`), the dashboard signs in through it (sessions, lockout, audit) and
needs the `admin:access` permission; the static `QCLI_ADMIN_USERNAME`/`QCLI_ADMIN_PASSWORD` are only used without it.

### Sign in with the host app's session

When the users plugin delegates authentication to your application (`CliUsersBuilder().set_auth_adapter(...)`), it
sets the server option `external_authentication` when mounted and the dashboard runs in **external** mode:

- `GET /api/v1/qcli/auth/mode` answers `{ "mode": "external", "signInUrl": "<sign_in_url>" }` with
  `Cache-Control: no-store`. It is public, also on protected servers, so the dashboard can call it before anyone signs
  in. Otherwise it answers `"password"`.
- The dashboard hides its username/password form and shows "Sign in at `<signInUrl>`" with a Retry button (or tells
  users to sign in through the host application when no sign-in URL is set). Any 401 leads to that screen.
- `POST /api/v1/qcli/auth/login` signs no one in: it answers `501` with `code: "EXTERNAL_AUTH"`, a message pointing
  to the host's sign-in and `signInUrl`. The static admin credentials are never accepted in this mode.
- The admin routes accept whatever your adapter accepts (for example the bearer token handed over below) and still
  need `admin:access` (`is_admin=True` grants it).

```python
admin_plugin = (
    CliAdminBuilder()
    .set_users_plugin(users_plugin)
    .set_sign_in_url("https://app.example.com/sign-in")
    .build(deps)
)
result.mount_plugin(admin_plugin)   # reads result.options.external_authentication
```

`set_sign_in_url()` takes an absolute `http`/`https` URL or a root-relative path (`/sign-in`); anything else (for
example `javascript:` or `//host`) raises `ValueError` in `build()`. The mode is read from the server's options:
`mount_plugin` hands them to the plugin; when you include the router yourself, pass
`AdminBuildDeps(server_options=result.options, ...)`.

**Handoff.** Send signed-in users to the dashboard with your bearer token in the URL fragment:

```
/qcli/admin/#token=<host bearer token>
```

A fragment is never sent to servers or written to access logs. The dashboard reads it on startup, stores it as its
admin token, removes it from the address bar (`history.replaceState`) and sends it as `Authorization: Bearer …` on
every API call, where your external auth adapter validates it like any other request from your app.

```python
# Host app: the adapter accepts the app's own JWT.
async def host_auth(request):
    claims = await verify_jwt(request.headers.get("authorization"))   # your own validation
    if claims is None:
        return None
    return ExternalAuthResult(
        user=User(id=claims["sub"], name=claims["name"], email=claims.get("email", "")),
        is_admin="admin" in claims.get("roles", []),
    )

users_plugin = CliUsersBuilder().set_auth_adapter(host_auth).build()
admin_plugin = CliAdminBuilder().set_users_plugin(users_plugin) \
    .set_sign_in_url("/sign-in?returnUrl=%2Fadmin%2Fconsole").build(deps)
```

```ts
// Host front end: an "Open console" button for a signed-in admin.
window.location.href = `/qcli/admin/#token=${encodeURIComponent(accessToken)}`;
```

When the token expires, the next API call answers 401 and the dashboard shows the sign-in screen; after signing in
again, the host repeats the handoff.

## Dashboard

The plugin can serve an optional SPA dashboard. Install the `@qodalis/cli-server-dashboard` npm package and the plugin will auto-detect the built assets, or set a custom path via `set_dashboard_path()`.

## Color scheme

Match the dashboard to your own design with `set_theme()`:

```python
admin_plugin = (
    CliAdminBuilder()
    .set_credentials("admin", "my-secret-password")
    .set_theme({
        "mode": "system",
        "light": {
            "primary": "#2a6bba", "primaryHover": "#1f5596", "accent": "#2a6bba",
            "background": "#f1f6fa", "backgroundAccent": "#f1f6fa", "surface": "#ffffff",
            "border": "#d9e3ec", "heading": "#142231", "text": "#142231", "muted": "#5d6f82",
        },
        "dark": {
            "primary": "#4f9be0", "primaryHover": "#3f8ad3", "onPrimary": "#0d1823", "accent": "#4f9be0",
            "background": "#0d1823", "backgroundAccent": "#0d1823", "surface": "#13212f", "popover": "#13212f",
            "border": "#22364a", "text": "#e6eef6", "muted": "#93a6b9",
        },
    })
    .build(deps)
)
```

Token keys are camelCase as on the wire, or snake_case (`primary_hover`). `set_theme()` raises `ValueError` on an
invalid theme; `validate_admin_theme()` and `ADMIN_THEME_TOKENS` are exported.

`mode` is `dark` (default), `light`, or `system` to follow the browser's `prefers-color-scheme`. Each scheme takes any
subset of the tokens below; the ones you leave out keep their defaults.

Values must be plain CSS colors: hex (`#rgb`, `#rgba`, `#rrggbb`, `#rrggbbaa`), a color function with numeric arguments
(`rgb()`, `rgba()`, `hsl()`, `hsla()`, `hwb()`, `lab()`, `lch()`, `oklab()`, `oklch()`) or a named color. Anything else
(an unknown mode or token, `var()`, `url()`, `;` ...) is rejected at startup with an error naming the token, so a theme
cannot inject CSS.

The plugin serves the validated scheme at `GET /qcli/admin/theme.json` (public, like the dashboard files, since the
sign-in page uses it), e.g. `{"mode":"system","dark":{"primary":"#4f9be0"},"light":{"primary":"#2a6bba"}}`. The
dashboard applies it before its first render.

The defaults match the Qodalis CLI panel: its grays, indigo accent with the violet glow, and status colors in dark; the same palette for light backgrounds in light.

| Token | CSS variable | Used for | Dark default | Light default |
| --- | --- | --- | --- | --- |
| `primary` | `--qcli-primary` | Primary buttons, active navigation, toggles, focus rings | `#818cf8` | `#4f46e5` |
| `primaryHover` | `--qcli-primary-hover` | Primary buttons on hover | `#a5b4fc` | `#4338ca` |
| `onPrimary` | `--qcli-on-primary` | Text and icons on `primary` | `#111827` | `#ffffff` |
| `accent` | `--qcli-accent` | Highlighted text: links, command names, active items | `#a78bfa` | `#7c3aed` |
| `background` | `--qcli-background` | Page background | `#111827` | `#f9fafb` |
| `backgroundAccent` | `--qcli-background-accent` | Start of the page background gradient (set it to `background` for a flat page) | `#1f2937` | `#eef2ff` |
| `sidebar` | `--qcli-sidebar` | Sidebar background | `#1f2937` | `#ffffff` |
| `surface` | `--qcli-surface` | Cards, panels and tables | `rgba(255, 255, 255, 0.04)` | `#ffffff` |
| `popover` | `--qcli-popover` | Dialogs and dropdown menus (opaque) | `#1f2937` | `#ffffff` |
| `control` | `--qcli-control` | Inputs, secondary buttons, chips | `rgba(255, 255, 255, 0.06)` | `rgba(17, 24, 39, 0.05)` |
| `controlHover` | `--qcli-control-hover` | Inputs and secondary buttons on hover | `rgba(255, 255, 255, 0.1)` | `rgba(17, 24, 39, 0.1)` |
| `border` | `--qcli-border` | Borders and dividers | `#374151` | `#e5e7eb` |
| `overlay` | `--qcli-overlay` | Backdrop behind dialogs | `rgba(0, 0, 0, 0.6)` | `rgba(17, 24, 39, 0.45)` |
| `heading` | `--qcli-heading` | Headings and values | `#ffffff` | `#111827` |
| `text` | `--qcli-text` | Body text | `rgba(255, 255, 255, 0.87)` | `#1f2937` |
| `muted` | `--qcli-muted` | Secondary text and labels | `rgba(255, 255, 255, 0.6)` | `#4b5563` |
| `subtle` | `--qcli-subtle` | Hints, placeholders, timestamps | `rgba(255, 255, 255, 0.4)` | `#6b7280` |
| `success` | `--qcli-success` | Success states | `#3fb950` | `#1a7f37` |
| `warning` | `--qcli-warning` | Warning states | `#f0883e` | `#bc4c00` |
| `error` | `--qcli-error` | Errors and destructive actions | `#f85149` | `#cf222e` |
| `info` | `--qcli-info` | Informational states | `#58a6ff` | `#0969da` |

## Dependencies

- `PyJWT>=2.0.0`
- `fastapi>=0.100.0`
- `psutil>=5.9.0`

## License

MIT
