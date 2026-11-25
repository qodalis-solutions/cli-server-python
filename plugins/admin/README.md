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

Once mounted, every log line the plugin captures is published on the server's event bus as `log:entry` (`timestamp`,
`level`, `message`, `source`; audience `logs:read`), which the dashboard's Logs page follows live. `AdminBuildDeps`
takes `event_bus` to name a bus, or `broadcast_fn` (an async function receiving the JSON message) to send the entries
yourself.

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
| GET | `/status` | Server status, uptime, process memory (`memoryUsageMb`), the machine's memory (`systemMemoryTotalMb`, `systemMemoryUsedMb`, `memoryLimited`), connected clients, enabled features |
| GET | `/status/history?minutes=` | Status samples (every 15 s, kept 24 h in memory), oldest first: `memoryMb`, `systemMemoryUsedMb`, `systemMemoryTotalMb`, `cpuPercent`, `activeWsConnections`, `activeShellSessions`; `minutes` defaults to 60, clamped to 1..1440 |
| GET | `/plugins` | List registered plugin modules |
| GET | `/plugins/installed` | List the installed server plugin packages (`id`, `name`, `version`, `description`) |
| GET | `/plugins/{id}` | One plugin module by name (ignoring case); 404 `PLUGIN_NOT_FOUND` |
| POST | `/plugins/{id}/toggle` | Enable or disable a plugin module |
| GET | `/config` | The store providers use unless they have their own and the server's config providers the caller may see (`config:read`), each with its `storage` and, when it saves to a store, its `store`: `{"defaultStore": {"kind", "title", "detail", "persistent"}, "providers": [...]}` |
| GET | `/config/{provider}` | One config provider, with its `store` |
| PATCH | `/config/{provider}/{section}` | Change settings (`config:write`): `{"changes": {...}, "version": "3"}`; 400 per-key errors, 409 stale version |
| GET | `/logs` | Retrieve buffered log entries: `?level=` (one or comma-separated `DEBUG`, `INFO`, `WARN`, `ERROR`), `search=` (message or source), `service=`, `from=`/`to=` (ISO 8601, no offset = UTC), `limit=`, `offset=`; answers `entries`, `total`, `limit`, `offset`, `hasMore`, `store` (`memory`), `persistent` (`false`), `services` (`[]`) |
| GET | `/logs/histogram` | Buffered log entries per level in equal time columns: `?from=&to=&buckets=` (default the last 24 h in 24 columns, `buckets` clamped to 1..200) with the filters of `/logs` |
| GET | `/ws/clients` | List connected WebSocket clients |

`systemMemoryTotalMb` is the RAM the server can use: the container (cgroup) limit when one is set below the installed RAM (`memory.max`, v1 `memory.limit_in_bytes`), then `memoryLimited` is true, otherwise the installed RAM (`psutil.virtual_memory().total`). `systemMemoryUsedMb` is the memory in use: the container's usage when limited (`memory.current`, v1 `memory.usage_in_bytes`), otherwise total minus available (`psutil.virtual_memory().available`, MemAvailable of `/proc/meminfo` on Linux). Each is null when it cannot be read.

The status sampler runs with the app's lifespan once the plugin is mounted (`result.mount_plugin(admin)`), first
sample at start, and is cancelled on shutdown. `cpuPercent` is the process CPU time over the last interval as a
percentage of one core (`null` for the first sample). Bad `from`/`to`/`buckets`/`level`/`minutes` values answer
`400 { "error": "..." }`.

With the users plugin (`set_users_plugin(...)`), the dashboard signs in through it (sessions, lockout, audit) and
needs the `admin:access` permission; the static `QCLI_ADMIN_USERNAME`/`QCLI_ADMIN_PASSWORD` are only used without it.

### Sign in with the host app's session

The full setup (adapter, middleware order, permissions) is in [Use with your app's sign-in](../../README.md#use-with-your-apps-sign-in).

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

**Coming back.** The dashboard (1.0.0-beta.23 and later) sends users to the sign-in URL with the dashboard page they
were on (for example a terminal link's approval page): it fills `{returnTo}` in the URL
(`https://app.example.com/sign-in?next={returnTo}`), or appends a `returnTo=` parameter. After signing the user in,
the host opens that URL with `#token=<host bearer token>` (see the handoff below).

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

## Config providers

The `/config` routes serve the server's config providers (`builder.config`; see the
[server README](https://github.com/qodalis-solutions/cli-server-python#config-providers)). The plugin adds two: `auth`
(read-only: the dashboard's sign-in mode, the admin username, whether the default credentials or an explicit JWT
secret are in use, the sign-in URL; never a password or the secret) and `logs` (the log buffer's `capacity`,
100-100000, and its lowest level `minLevel`, changeable at runtime; kept in the server's default config store, or in
its own with `CliAdminBuilder().set_config_store(store)`). They are registered with the server when the
plugin is built (`config_providers` on the result). On a server protected by the users plugin the routes need
`config:read` (GET) and `config:write` (PATCH); a users session's permissions are checked too, while the static admin
of password mode may do everything. The former `GET`/`PUT /config` and `AdminConfig.get_config_sections`/
`update_settings` are gone: the free settings live in the `custom` provider.

## Command modules

The Plugins page also lists the command modules, each with its commands and a switch that disables them
(`/api/v1/qcli/plugins`). A module added with `builder.add_module(...)` is listed with its name, version, description
and `icon` (one emoji or an image URL). Commands registered on their own (`builder.add_processor(...)`) are listed
under the module their `module` attribute names (ignoring case); without such a module, the group takes its version
and description from the commands' package (empty for the application's own code). To describe such a group, add a
module of the same name with no processors of its own:

```python
class ShopModule(CliModule):
    name = "shop"
    version = "1.0.0"
    description = "Shop administration"
    icon = "🛒"
    processors = []  # StatsCommand etc. set module = "shop"


builder.add_module(ShopModule()).add_processor(StatsCommand()).add_processor(OrdersCommand())
```

## Installed plugins

The dashboard's Plugins page lists the server plugins the host installed: the core server, and each plugin package
mounted with `result.mount_plugin(...)` (users, admin, jobs) or added through the builder (`add_module`,
`set_file_storage_provider`, `add_filesystem`, `add_data_explorer_provider`), with the distribution's version and
summary. A plugin of your own lists itself:

```python
builder.add_installed_plugin("acme-cli-billing")        # a distribution name
builder.add_installed_plugin(BillingModule())           # or an object of the package
```

The status reports the `audit` feature when the audit plugin is installed (the dashboard's Audit page; the users plugin
mounts it), and `jobs`
when the jobs plugin is.

## Dashboard

The plugin can serve an optional SPA dashboard. Install the `@qodalis/cli-server-dashboard` npm package and the plugin will auto-detect the built assets, or set a custom path via `set_dashboard_path()`.

## Dashboard plugins from the marketplace

A *dashboard plugin* is an npm package whose bundle is a dashboard extension (`extension.js`, optional
`extension.css`, built with `@qodalis/cli-server-dashboard-kit`'s `defineExtension`), listed in the
[plugin marketplace](https://plugins.qodalis.com) with `kind: "dashboard"`. The admin plugin lists them, installs,
updates and removes them at runtime, and serves them from the server's own origin. A plugin runs inside the dashboard
with the signed-in user's rights, so by default only verified publishers' plugins can be installed. The marketplace is
on by default; the same contract and JSON as the Node and .NET servers.

```python
from qodalis_cli_admin import CliAdminBuilder, DashboardMarketplaceOptions

admin = (
    CliAdminBuilder()
    .set_users_plugin(users_plugin)
    .set_marketplace(DashboardMarketplaceOptions(
        url="https://plugins.qodalis.com",   # default
        install="verified",                  # default; "listed": any listed version; "off": browse only
        dir="./qcli-data/dashboard-plugins",  # default, from the working directory
    ))
    .build(AdminBuildDeps(registry=..., event_socket_manager=..., builder=...))
)
```

| Option | Default | Description |
|---|---|---|
| `url` | `https://plugins.qodalis.com` | The marketplace (its public API at `/api/v1/qcli/marketplace`) |
| `install` | `verified` | `verified`: listed versions of verified publishers only; `listed`: any listed version; `off`: browse only |
| `dir` | `./qcli-data/dashboard-plugins` | Where installed plugins are kept |
| `fetch` | the standard library (`urllib`, in a thread) | The HTTP client for the marketplace and the CDNs: `async fetch(url, headers, timeout) -> MarketplaceResponse(status, body, headers)` (tests pass a fake) |
| `cdns` | jsDelivr, unpkg | Where bundles are downloaded from, in order |
| `timeout` | `15` | Timeout of each request, in seconds |

`set_marketplace` also takes a dict of these fields. `set_marketplace(False)` serves none of the routes below, adds no
`admin dashboard` command and loads no installed plugin. An invalid `url` or `install` raises `ValueError` in `build`.

**Installing** reads the plugin (`GET /plugins/{name}` of the marketplace) and takes the newest listed version this
dashboard runs (`dashboardApi` 1 or less) unless one is given, checks the trust policy, downloads each file of the
version record (`files: [{ path, integrity, size }]`, read from `/plugins/{name}/versions/{version}` when the details
leave them out) from jsDelivr, then unpkg (5 MB at most each), and checks its bytes against the marketplace's `sha384-`
integrity before anything is written: a mismatch on every CDN is a 502 and stores nothing. The files are stored as
`<dir>/<name>/<version>/extension.js|css` (a scoped name `@scope/name` as the directory `@scope+name`; names and
versions are validated, so nothing leaves `dir`) and listed in `<dir>/installed.json`, written atomically (a temporary
file renamed over it). Changes run one at a time.

```json
{
  "plugins": [
    {
      "name": "@qodalis/cli-dashboard-module-jobs",
      "version": "1.1.0",
      "id": "jobs",
      "title": "Jobs",
      "icon": "⏱️",
      "files": [
        { "file": "extension.js", "path": "dist/extension.js", "integrity": "sha384-…", "size": 48211 },
        { "file": "extension.css", "path": "dist/extension.css", "integrity": "sha384-…", "size": 5120 }
      ],
      "installedAt": "2026-11-25T10:04:05.123Z",
      "installedBy": "admin"
    }
  ]
}
```

The plugin is then registered as a dashboard extension at once (no restart): its page id is the package's suffix for
`@qodalis/cli-dashboard-module-<id>`, else the extension's own id (`extension.id` of the version record, then of the
plugin), else the package name without its scope and `cli-dashboard-` prefix. An id another extension already has is a
409. Its files are served at `/qcli/modules/<id>/extension.js|css` (no sign-in, `Cache-Control: no-cache` with an ETag,
the entry carrying `?v=<hash>`; the plugin's `asset_routes`, mounted by `mount_plugin`), and
`GET /dashboard/extensions` lists it with `source: "marketplace"`. An update keeps the page id and deletes the
previous version's files (updating to the installed version does nothing); a removal unregisters the page and deletes
the files. `installedBy` is the signed-in user, `anonymous` for a command on a server without sign-in.

At startup the plugin registers what `installed.json` lists, without the marketplace; a plugin whose files are missing
or no longer match their integrity, or whose id another extension took, is skipped with a warning. Plugins mounted
after the admin plugin cannot take a marketplace page's id, so mount the admin plugin after the plugins with pages of
their own.

Every change is audited (`dashboard.plugin.install`, `dashboard.plugin.update`, `dashboard.plugin.remove`: target the
package, `properties: { name, version, id, previousVersion? }`) in the server's audit log (the users plugin mounts it;
or the store set with `builder.set_config_audit_store`) and published as `dashboard:extensions-changed`
(`{ action, name, version, id, previousVersion? }`, audience `admin:access`), so open dashboards reload their pages.

### Marketplace API

Mounted at `/api/v1/qcli/dashboard/marketplace`. Reading needs `admin:access`; `POST`, `PUT` and `DELETE` need
`dashboard:install` (core catalog, group Server; the static admin of password sign-in holds every permission). Errors
are `{ "error": "..." }`.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | `{ "marketplaceUrl": "https://plugins.qodalis.com", "install": "verified", "dashboardApi": 1 }` |
| GET | `/plugins?query=&page=&pageSize=` | The marketplace's list asked with `kind=dashboard&dashboardApi=1` (`{ items, total, page, pageSize }`); items of another kind are dropped (an older marketplace ignores `kind`; `total` is then the number of dashboard items), each item annotated (below); 502 `{ "error": "marketplace unreachable" }` |
| GET | `/plugins/{name}` | One plugin with its `versions`, annotated; each version also `allowed` and `blockedReason`; 404 when not listed |
| GET | `/installed` | The installed plugins (below); `updateAvailable` when the marketplace answers |
| POST | `/installed` | `{ name, version? }`: 201 with the installed plugin; 400 invalid name or version, `needs dashboard API 2`; 403 `publisher not verified`, `install disabled`; 404 unknown plugin or version; 409 already installed, id taken; 502 `marketplace unreachable`, `integrity mismatch: <path>`, `download failed: <path>` |
| PUT | `/installed/{name}` | `{ version? }`: updates (default: the newest fitting version), same checks; 404 when not installed |
| DELETE | `/installed/{name}` | Removes the plugin; 404 when not installed |

Scoped names go in the path raw (`/installed/@acme/tickets`) or encoded (`/installed/%40acme%2Ftickets`).

A `GET /plugins` item is the marketplace's item plus:

```json
{
  "name": "@qodalis/cli-dashboard-module-jobs",
  "kind": "dashboard",
  "publisher": { "name": "@qodalis", "verified": true },
  "version": "1.1.0",
  "installed": { "version": "1.0.0" },
  "updateAvailable": true,
  "allowed": true,
  "requires": { "serverPackage": "qodalis-cli-server-plugin-jobs", "installed": true }
}
```

`installed` is null when not installed; `blockedReason` (only when `allowed` is false) is `install disabled`,
`unverified publisher` or `needs dashboard API <n>` (a version of the details may also be `version flagged`);
`requires` is null when the plugin names no Python server package (`serverPackages.python`, or the `pypi` entry of
the list form `[{ "ecosystem": "pypi", "name": ... }]`), else whether the server has it (`GET /plugins/installed`).

`GET /installed`:

```json
[
  {
    "name": "@qodalis/cli-dashboard-module-jobs",
    "version": "1.1.0",
    "title": "Jobs",
    "icon": "⏱️",
    "id": "jobs",
    "installedAt": "2026-11-25T10:04:05.123Z",
    "installedBy": "admin",
    "updateAvailable": false
  }
]
```

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

## Name and icon

By default the dashboard is "Qodalis Cli Admin" with the Qodalis Solutions logo. An application that ships it
under its own name sets `set_branding()`:

```python
CliAdminBuilder().set_branding({
    "name": "RateBell",          # browser tab, sidebar, sign-in page
    "icon_url": "/favicon.svg",  # http(s) URL, root-relative path or base64 data:image URL
    "cli_name": "RateBell CLI",  # the terminal's welcome banner
})
```

Anything left unset keeps the Qodalis default. The terminal's copyright line, docs link and `version` stay Qodalis
CLI's. The values are served with the color scheme at `/qcli/admin/theme.json`; `set_branding()` raises
`ValueError` on an invalid value.

## Dependencies

- `PyJWT>=2.0.0`
- `fastapi>=0.100.0`
- `psutil>=5.9.0`

## Terminal commands

The plugin brings the `admin` command (registered by `mount_plugin`, which also lets the module registry filter the
executor). It reads the same services as the admin routes. On a protected server every sub-command needs
`admin:access`; `admin logs` also needs `logs:read`, `admin dashboard install|update|remove` also
`dashboard:install`. Server settings are the core
[`config`](../../README.md#config-providers) command. Lists take `--json` (the payload of the matching route).

| Command | Permission | Does |
|---|---|---|
| `admin`, `admin status [--json]` | `admin:access` | Server package and version, platform, uptime, memory, CPU, connections, commands, jobs, features (`GET /status`) |
| `admin status --history [--minutes 60] [--json]` | `admin:access` | Summary of the status samples of the last minutes (1-1440): count, min/avg/max memory and CPU, peak connections (`GET /status/history`) |
| `admin plugins`, `admin plugins list [--json]` | `admin:access` | Command modules: id, name, version, enabled or disabled, commands (`GET /plugins`) |
| `admin plugins enable <id>`, `admin plugins disable <id>` | `admin:access` | Turns a module's commands on or off, like `POST /plugins/{id}/toggle` but idempotent (`... is already enabled.`); the `admin` module itself cannot be disabled from the terminal |
| `admin clients [--json]` | `admin:access` | Connected WebSocket clients: id, type, connected since, address (`GET /ws/clients`) |
| `admin logs [--level warn,error] [--search <text>] [--service <source>] [--since 5m\|1h\|7d\|<ISO date>] [-n 50] [--json]` | `admin:access`, `logs:read` | The newest matching log entries (1-1000), oldest first, from the log buffer (`--service` matches the logger name) |
| `admin dashboard`, `admin dashboard list [--json]` | `admin:access` | The dashboard plugins installed from the marketplace: page id, package, version (and whether an update is available), when and by whom (`GET /dashboard/marketplace/installed`) |
| `admin dashboard search [<query>] [--json]` | `admin:access` | The marketplace's dashboard plugins: package, version, publisher, installed / installable / blocked and why, a missing server plugin (`GET /dashboard/marketplace/plugins`) |
| `admin dashboard install <name>[@version]` | `admin:access`, `dashboard:install` | Installs the newest version this dashboard runs, or the one given, with the API's checks (`POST /dashboard/marketplace/installed`) |
| `admin dashboard update <name>` | `admin:access`, `dashboard:install` | Updates to the newest version (`PUT /dashboard/marketplace/installed/{name}`) |
| `admin dashboard remove <name>` | `admin:access`, `dashboard:install` | Removes the page and its files (`DELETE /dashboard/marketplace/installed/{name}`) |

On a server without the users plugin, `/execute` is open, and so are these commands. To follow the log live, use
`server logs` of the web CLI plugin `@qodalis/cli-server-logs` (the `/ws/v1/qcli/logs` socket, `logs:read`).

```text
$ admin status
Server      qodalis-cli-server 1.0.0b31
Platform    python 3.13.1 (Linux 6.8.0)
Uptime      2 h 5 min (since 2026-11-25 08:00:00)
Memory      84.2 MB (process) · machine 9.8 GB of 16 GB used (61%)
CPU         1.3%
Connections 2 WebSocket, 0 shell
Commands    41
Jobs        0
Features    filesystem, jobs, audit

$ admin status --history --minutes 15
Last 15 min: 60 sample(s) every 15 s, 2026-11-25 09:50:00 to 2026-11-25 10:04:45
Memory      min 80.1 MB  avg 82.4 MB  max 85.3 MB
CPU         min 0.2%  avg 1.1%  max 5.4%
Peak        3 WebSocket, 1 shell

$ admin plugins disable weather
Disabled weather (1 command).

$ admin logs --level warn,error -n 2
Last 2 of 7 entries
2026-11-25 10:01:12 WARN  [jobs] cleanup took 12 s
2026-11-25 10:03:40 ERROR [db] connection timeout

$ admin dashboard search
Package                             Version  Publisher            Status
@qodalis/cli-dashboard-module-jobs  1.1.0    @qodalis (verified)  installable
@acme/cli-dashboard-tickets         2.0.0    @acme                blocked: unverified publisher

$ admin dashboard install @qodalis/cli-dashboard-module-jobs@1.0.0
Installed @qodalis/cli-dashboard-module-jobs@1.0.0: the dashboard page "Jobs" (/x/jobs).

$ admin dashboard
1 dashboard plugin(s) from https://plugins.qodalis.com (install: verified)
ID    Package                             Version                   Installed            By
jobs  @qodalis/cli-dashboard-module-jobs  1.0.0 (update available)  2026-11-25 10:04:05  admin

$ admin dashboard update @qodalis/cli-dashboard-module-jobs
Updated @qodalis/cli-dashboard-module-jobs from 1.0.0 to 1.1.0.

$ admin dashboard remove @qodalis/cli-dashboard-module-jobs
Removed @qodalis/cli-dashboard-module-jobs@1.1.0 (the page jobs).

$ admin dashboard install @acme/cli-dashboard-tickets
Error: publisher not verified
```

## License

MIT
