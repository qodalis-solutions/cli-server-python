# Change Log

Notable changes to the Python CLI server packages (`qodalis-cli-server`, `qodalis-cli-server-abstractions`,
`qodalis-cli-server-plugin-*`). Every package shares one version with the .NET and Node servers (`1.0.0b19` is
PEP 440 for `1.0.0-beta.19`).

## Unreleased

- **Restart required and Restart server** (ported from the Node server, same endpoints and JSON). Core keeps the changes waiting for a restart (`builder.restart` / `result.restart`: `mark_required(id, source, description)`, idempotent by id, `clear_required(id)`, empty on every start); the config service marks a changed `requires_restart` entry as `config:<provider>.<section>.<key>` and clears it when the value is set back. `CliServerOptions(restart=CliServerRestartOptions(mode, exit_code, handler, allow_anonymous, drain_timeout, runtime))`: `auto` (default; `exit` when PM2, systemd, Kubernetes or Docker is detected, else not supported), `exit` (exit code 75 by default, for the supervisor), `self` (the same command run again in this process: `sys.executable` with `sys.orig_argv`, same environment and working directory), `handler`. Graceful shutdown: the uvicorn server (found through its signal handler, or `restart.attach_server`) stops accepting, drains in-flight requests up to 10 s and closes WebSockets with 1012; the app's lifespan shutdown then closes the remaining sockets with 1012, stops mounted job schedulers, calls mounted plugins' `dispose()` and `restart.on_shutdown(name, fn)` hooks, then runs the mode's action (without uvicorn the service drains and closes on its own). `GET /api/v1/qcli/server/restart` (`admin:access`): `{ supported, mode, supervisor, reason?, startedAt, pending: [{ id, source, description, since }] }`; `POST` `{ reason? }` (new permission **`server:restart`**, core catalog, group Server): 202 `{ restarting: true, pending }`, 409 when not supported or already restarting, 403 on a server without sign-in unless `allow_anonymous`. Audit `server.restart`, events `server:restart-required` and `server:restarting` (audience `admin:access`). The events, logs and shell sockets' `broadcast_disconnect` / `close_all` take a close code. Admin plugin: `admin restart [--yes] [--reason]` and `admin restart status [--json]`. The demo restarts in `self` mode; the runtime is injectable, so tests never exit or exec.
- **Settings of dashboard plugins** (ported from the Node server). A marketplace dashboard plugin's `config.entries` (the config providers' entry model, no `secret`, at most 50 entries, 32 KB, defaults valid) become a section of the new config provider **`dashboard-plugins`** (one per installed plugin with settings; its store resolved like every provider's: the host's store for `dashboard-plugins`, else the default store). `POST /installed` and `PUT /installed/{name}` take `config: { key: value }`, checked before anything is downloaded (400 `validation failed` with per-key `errors`); an update keeps the stored values the new version still accepts and answers `configDropped`; a removal deletes the section's values; the entries are kept in `installed.json` so sections come back after a restart offline; a version whose settings are invalid installs without them, with `warnings`. `GET /installed/{name}/config` answers `{ values, version }`; installed plugins carry `hasConfig`; changes made by an install, update or removal publish `config:changed`. `admin dashboard install|update --config '<json>'` and `admin dashboard info <name>`.
- Tests: exit diagnostics in `tests/conftest.py` (non-daemon threads still running after the session, listed with their stacks; every thread's stack every minute while the process has not exited).
- **Dashboard plugins from the marketplace** (ported from the Node server, same endpoints and JSON). The admin plugin lists the marketplace's `kind=dashboard` plugins and installs, updates and removes them at runtime: `set_marketplace(DashboardMarketplaceOptions(url="https://plugins.qodalis.com", install="verified" | "listed" | "off", dir="./qcli-data/dashboard-plugins", fetch=None))` (on by default; `False` turns it off). Routes under `/api/v1/qcli/dashboard/marketplace`: `GET /` (`{marketplaceUrl, install, dashboardApi: 1}`), `GET /plugins?query=&page=&pageSize=` (asked with `kind=dashboard&dashboardApi=1`, other kinds dropped) and `GET /plugins/{name}` (the marketplace's items with `installed`, `updateAvailable`, `allowed`, `blockedReason` incl. `version flagged`, `requires: {serverPackage, installed}` from `serverPackages.python` or the `pypi` entry; 502 `marketplace unreachable`), `GET /installed`, `POST /installed` `{name, version?}` (201; the newest listed version with `dashboardApi` <= 1), `PUT /installed/{name}` (same version: 200, nothing changed), `DELETE /installed/{name}`. Bundles come from jsDelivr, then unpkg (5 MB each), and are checked against the marketplace's sha384 before anything is stored (a mismatch is a 502); they are kept in `<dir>/<name>/<version>/` (`@scope/name` as `@scope+name`) with `<dir>/installed.json` written atomically, registered as dashboard extensions at once (`GET /dashboard/extensions` marks them `source: "marketplace"`; an id already registered is a 409; updates keep the id), served at `/qcli/modules/<id>/extension.js|css` with an ETag, and registered again at startup (files whose hash changed and taken ids skipped). Changes run one at a time, need the new permission **`dashboard:install`** (core catalog, group Server), are audited as `dashboard.plugin.install|update|remove` and published as `dashboard:extensions-changed` (audience `admin:access`). Terminal: `admin dashboard [list|search <query>|install <name>[@version]|update <name>|remove <name>]`.
- Core: `builder.remove_dashboard_extension(id)` (extensions added and removed at runtime; the extensions list reads them live), `builder.audit_sink` (the audit sink of the config changes, for plugins' own changes), and a mounted plugin's `asset_routes` (`prefix`, `app` and an optional `handles(path)`: files served outside the API without sign-in, other routes under the prefix answering what it declines).
- **Config providers** (#80, cli-server-dashboard#61; the same contract and JSON as the Node and .NET servers). The server's configuration is read and changed through providers that the server, plugins and the host register (`builder.add_config_provider`, a module's `config_providers`, a plugin's `config_providers`): `ICliConfigProvider` (read) and `ICliConfigWriter` (write) in the abstractions, with entries typed `string`, `number`, `boolean`, `string[]`, `enum`, `secret`, `json`, `duration` or `url`, sources, `mutable`, `requires_restart`, `sensitive`, validation (`min`, `max`, `pattern`, `options`, `required`) and an `editor` hint. `CliStoredConfigProvider` describes settings in code and keeps changes in the server's config store: `InMemoryConfigStore` (default), `JsonFileConfigStore` or the databases plugin's `CliDatabaseConfigStore` (table `QcliConfig`, migration source `qcli-config`); `builder.set_config_store`, `CliServerOptions(config=CliServerConfigOptions(store, environment, command))`, `result.config.ready()`. Built-in providers: `server`, `host` (options and an allowlist of environment variables, secrets masked), `cors` (allowed origins at runtime), `mcp` (tool lists, instructions, read-only), `filesystem` (upload limit), `custom`, and from plugins `auth` and `logs` (admin), `jobs` (history retention) and `users` (sessions, lockout, passwords).
- **The admin plugin's config API is converted**: `GET /api/v1/qcli/config` answers `{providers}`, `GET /config/{provider}` one provider, `PATCH /config/{provider}/{section}` with `{changes, version}` changes a section (400 with per-key errors, 409 with the current version when stale). The former `GET`/`PUT` and `AdminConfig`'s `get_config_sections`, `update_settings` and `get_settings` are gone. Secrets are never returned (`value: null`, `isSet`); `null` resets a setting.
- **Permissions `config:read` and `config:write`** in core's catalog (plus a provider's `required_permission`); every change is audited as `config.update` (old and new values, secrets as `***`) and published as `config:changed` (audience `config:read`).
- **`config` command** (`list`, `get`, `set ... [--expected-version v]`, `reset`) and **MCP tools** `config_list`, `config_get`, `config_set`. New MCP option `CliMcpOptions.read_only`: only tools annotated read-only are listed and callable.
- **Plugin terminal commands** (ported from the Node server). Admin plugin: `admin status [--history [--minutes N]]`, `admin plugins [list|enable|disable <id>]` (idempotent, the admin module itself not disabled), `admin clients`, `admin logs [--level] [--search] [--service] [--since 5m|1h|7d|<ISO>] [-n N]` (`admin:access`; `logs` also `logs:read`); the plugin has a `module`, and mounting it lets the module registry filter the executor. Databases plugin: `databases runs [<db>] [-n N]` (`databases:read`). Docs plugin: `docs list [--group]`, `docs read <slug>` (no permission, like the pages); the plugin has a `module`. Public-api plugin: `api usage [--user <name|id>] [--days N]` (your own; `tokens:read` for another account); the plugin has a `module`. Lists take `--json`. Shared services: `status_snapshot` (`GET /status` now has `cpuUsagePercent`), `summarize_status_history`, `ModuleRegistry.set_enabled`, `parse_since`.
- **AWS permissions**: `aws:read` on the `aws` command, `aws:write` on `s3 cp|rm|mb|rb`, `ec2 start|stop|reboot`, `lambda invoke`, `sqs send|purge`, `sns publish` and `configure set`, in the catalog under group `AWS`.
- **`ICliModule.permissions`**: a module's permissions, added to the catalog by `add_module` (surface `cli`; a name already there stays as registered).
- **The machine's memory in the admin status** (as on the Node server): `GET /api/v1/qcli/status` and `admin status --json` add `systemMemoryTotalMb` (the RAM the server can use: a cgroup limit below the installed RAM, else the installed RAM), `systemMemoryUsedMb` (the cgroup's usage when limited, else total minus available, from psutil, already a dependency) and `memoryLimited`; each status history sample adds `systemMemoryUsedMb` and `systemMemoryTotalMb`. None when unknown. `admin status` shows `Memory      146 MB (process) · machine 9.8 GB of 16 GB used (61%)` (or `container 300 MB of 512 MB used (59%)`).
- Every plugin README has a `Terminal commands` section (server-side commands, or the web CLI plugin commands that drive the plugin).
- The allowed origins are one live list (`result.allowed_origins`: CORS, WebSocket upgrades, MCP, the users plugin's CSRF check); the filesystem API reads its upload limit per upload; `LogRingBuffer.resize`/`min_level`; `CliJobScheduler.set_history_defaults`; the users plugin's session, lockout and password settings change at runtime.
- **A module's commands carry its name**: `GET /api/v1/qcli/commands` lists a command added with `add_module` under the module's name (`module`) unless the command names one itself (`ICliCommandRegistry.register(processor, module=None)`, `module_of`), so `aws` shows `module: "aws"`. The demo's own commands (`echo`, `status`, `time`, `hello`, `math`, `system`, `hash`, `base64`) are the `demo` module (`demo/demo_module.py`): the dashboard's Plugins page lists them as the demo plugin, which can be disabled like the others. The built-in `http` command names `core`.
- **Commands list their permissions** (the same JSON as the Node and .NET servers): `GET /api/v1/qcli/commands` gives every command and every sub-command in `processors` the permissions it declares itself as `requiredPermissions`, e.g. `aws:read` on `aws` and `aws:write` on `aws s3 rm`; left out when there are none. The parents' permissions and `commands:execute` are not repeated. The dashboard shows them on the Commands and plugin details pages.
- **Where settings come from and where changes are saved** (as on the Node server). `GET /api/v1/qcli/config` adds `store` (`{kind: memory|file|database|custom, title, detail, persistent}`) and every provider view `storage` (`{kind: store|runtime|options|environment|configuration|custom, description, sources}`). Stores describe themselves with the optional `ICliConfigStore.describe()` (`CliConfigStoreDescription`): `InMemoryConfigStore` `In memory` (not persistent), `JsonFileConfigStore` `JSON file` with the path relative to the working directory (or the file name only), `CliDatabaseConfigStore` `Database` with the dialect, database name and table (`PostgreSQL database qcli, table QcliConfig`; new argument `database`, `connection.database` from the drivers or the adapters' `database` argument, `cli_database_name`), never credentials; a store without it is a `custom` store named after its class (`describe_cli_config_store`). Providers declare the optional `storage` (`CliConfigProviderStorage`): `CliStoredConfigProvider` `store`, `server` `runtime`, `host` `options` (sources `Server options`, `Environment variables`), `auth` `options`; others `custom`. `ICliConfigService.describe_store()`; `config list` starts with `Store: JSON file (qcli-demo-config.json), kept across restarts` (or `Store: In memory, changes are lost on restart`) and shows read-only providers' sources; MCP `config_list` returns `store` and each provider's `storage`.
- **The portal plugin's `portal` command and config provider** (as on the Node server). `portal` (= `portal status`) `[--json]`, `config:read`: the mount, whether the site was found and where from (`set_site_dir`, `@qodalis/cli-server-portal`, the sibling checkout or the bundled copy; `resolve_site`), the sections (docs with its page count, the API's title and OpenAPI URL) and the current branding; `--json` is `portal.json` with `docs.pageCount` and `site: {path, found, source, dir}`. The `portal` config provider: `branding` (`name`, `logoUrl`, `themeMode`, `footerLinks`; the builder's values are the defaults, changes apply to the next `/portal.json` without a restart) and the read-only `site` (`path`, `siteFound`, `sections`); the theme colours stay in code. The built plugin has `config_providers`, `module`, `site_source` and `status()`.
- **A config store per provider** (as on the Node server): settings can live in several stores at once, some in the app's settings file, some in a database, some in Redis. A provider's store is, in this order: the host's store for its id (`CliServerConfigOptions(stores={"jobs": redis_store})`, `builder.set_config_store(store, "jobs")`), the provider's own (`CliStoredConfigProvider(..., store=store)`, `provider.set_store(store)`, `create_custom_config_provider(store=store)`, or `set_config_store(store)` on `CliJobsBuilder`, `CliUsersBuilder`, `CliPortalBuilder` and `CliAdminBuilder` for `jobs`, `users`, `portal` and `logs`), the default store (`CliServerConfigOptions(store=...)`, `builder.set_config_store(store)`), else memory. One store can serve several providers (keyed by provider and section); versions and conflicts stay per store and section. A store for an id can be set after the server is created, until that provider is registered. `result.config.ready()` waits for every store (and a store's optional `ready`). **Breaking (unreleased):** `GET /api/v1/qcli/config` answers `defaultStore` instead of `store`, and every stored provider (also `GET /config/{provider}` and MCP `config_list`/`config_get`) its own `store` (`{kind, title, detail, persistent}`; none on read-only providers); `ICliConfigService.describe_store()` is `describe_default_store()`; store kind `redis`; `describe_cli_config_store_name`. `config list` starts with `Default store: JSON file (qcli-demo-config.json), kept across restarts` and ends each stored provider's line with its store (`jobs  Jobs  · Redis (cache:6379/0, prefix qcli:config)`). The audit entry `config.update` (`properties.store`) and the `config:changed` event (`store`) carry the kind of store written.
- **`JsonFileConfigStore` in a section of the app's settings file**: `JsonFileConfigStore("config/app.json", section="qcli")` (nested `app.qcli` or `app:qcli`) keeps the settings under that key: the file is re-read on every write and only the section replaced (other keys, their order, the indentation and the file's mode kept; written to a temporary file and renamed); described as `JSON file (config/app.json, section qcli)`.
- **New package `qodalis-cli-server-plugin-config-redis`**: `CliRedisConfigStore(url=... | client=..., key_prefix, ttl_seconds, max_retries)` (redis-py's asyncio client, `redis>=5`) keeps settings in one Redis hash per provider section (`qcli:config:<provider>:<section>`: `values` as JSON, `version`, `updatedAt`); a write is checked against the version by the same Lua script as the Node server (`CliConfigConflictError` on a stale expected version, retried without one), so servers sharing the Redis never lose a change. Described by host, port, database and key prefix (`cache:6379/0, prefix qcli:config`), never the password.
- The demo keeps its settings in up to three stores: the JSON file (`QCLI_DEMO_CONFIG`) by default, `jobs` in a SQLite database when `QCLI_DEMO_DB` is set (`CliDatabaseConfigStore`) and the `demo` provider in Redis when `QCLI_DEMO_REDIS_URL` is set.
- **Breaking**: `CliEchoCommandProcessor`, `CliStatusCommandProcessor`, `CliSystemCommandProcessor`, `CliHashCommandProcessor` and `CliBase64CommandProcessor` are removed from `qodalis_cli` (they moved to the demo app, `demo/processors`), and so is the standalone server (`qodalis_cli/server.py`, the `qodalis-cli-server` command and the root `Dockerfile`; the ghcr.io image is built from `demo/Dockerfile`). `http` stays in core and now requires `http:request`, a new core permission (group Commands, "Make HTTP requests from the server"): grant it to the users and tokens that run `http`.

## 1.0.0b31

- The admin plugin embeds dashboard 1.0.0-beta.28: plugin pages no longer change the dashboard's layout (the modules' styles are scoped to their own elements, so Server resources, Events and the other pages keep their columns); the portal plugin serves `@qodalis/cli-server-portal` 1.0.0-beta.28.

## 1.0.0b30

- The admin plugin embeds dashboard 1.0.0-beta.27 (plugins' pages as modules, Users and Account pages, a forced password change at sign-in, Qodalis CLI 2.8.0 in its Terminal); the portal plugin serves `@qodalis/cli-server-portal` 1.0.0-beta.27.
- **Users and Account pages.** The users plugin serves the dashboard's Users page (users, groups, sessions; `users:read`, at `/qcli/modules/users`) and Account page (the signed-in user's profile and password change; every user, at `/qcli/modules/account`), both from `@qodalis/cli-dashboard-module-users` (embedded in `qodalis_cli_users/dashboard_users`; `set_users_dashboard_dir(path | False)`), also when the host application signs users in. New dashboard extension group `account`: listed in the dashboard's account menu (the signed-in user's menu) instead of the sidebar.
- **Job history without logs in lists, and a run summary** (cli-server-dotnet#96, as on the .NET and Node servers). `JobExecutionQuery.include_logs` (default `False`) and `JobExecution.log_count` / `log_entry_count`: a store may list runs without their log entries and set `log_count` instead; `get_execution` (the run detail endpoint, `server jobs logs`, the dashboard's log panel) always has them, and the list endpoints' `logCount` uses the store's count. The scheduler asks for the logs when it finishes the runs a previous process left running. New `ICliJobStorageProvider.summarize_executions(since, job_ids=None)`: per job and status, the number of runs and the start of the newest one (`JobExecutionSummary`, `summarize_job_runs`); in memory in the in-memory store, by default from `query_executions` a page at a time. The in-memory store keeps the logs in its lists. (The Python server has no SQL job store, so there are no indexes to add.)
- **Dashboard pages move into the plugins** that serve their APIs: the dashboard's Jobs, Audit, API tokens,
  Filesystem and Data explorer pages are modules (`@qodalis/cli-dashboard-module-<name>`) embedded in
  `qodalis-cli-server-plugin-jobs`, `-audit`, `-users`, `-filesystem` and `-data-explorer`
  (`scripts/copy_dashboard.py`) and served at `/qcli/modules/<id>` as dashboard extensions, with the permissions the
  pages need (`jobs:read`, `audit:read`, `files:read`, `data:query`; tokens none). The core serves Filesystem and
  Data explorer with their APIs (`CliServerOptions.dashboard_pages` points at another build or leaves one out); the
  plugins have `set_dashboard_dir(path | False)`. A plugin lists its pages in `dashboard_pages`; the helper is
  `qodalis_cli_server_abstractions.dashboard_modules` (re-exported as `qodalis_cli.dashboard_modules`), and the
  databases plugin uses it too.
- **Dashboard extensions: `widgets` and `search`.** An extension may name slots it fills besides its page
  (`overview`: cards on the dashboard's Overview, `palette`: entries in its command palette) and a `search` path
  (`/?q=`) the command palette opens. Jobs declares both slots and a search, Audit a search, Databases `overview`.
- **Databases** (`qodalis-cli-server-plugin-databases`, `qodalis_cli_databases`, new): the server's PostgreSQL (`asyncpg`), MySQL/MariaDB (`aiomysql`), SQL Server (`pymssql`) and SQLite (`sqlite3`) databases, with their stats and migrations. `CliDatabasesBuilder().add_database(...).add_sql_folder(...).build()`, mounted with `mount_plugin`.
  - **Stats** (`GET /api/v1/qcli/databases/{db}/stats`, terminal `databases stats`, MCP `databases_stats`):
    - server version and uptime;
    - size (data, indexes, log);
    - connections (active, open, max);
    - cumulative counters (queries, transactions, commits, rollbacks, rows read and written; `countersScope` `database` or `server`);
    - cache hit ratio;
    - the 10 largest tables.

    Each group is read on its own, and one the user may not read is listed in `unavailable` (e.g. SQL Server without `VIEW SERVER STATE`). The list carries each database's `sizeBytes`.
  - **Migrations** come from a folder of SQL files (`NNNN_name.up.sql` / `.down.sql`, `-- qcli:no-transaction`) or from code (`CliMigration`: SQL, SQL per dialect or a coroutine).
    - They apply in order under a per-database lock (`pg_advisory_lock`, `GET_LOCK`, `sp_getapplock`), each in a transaction with its history row.
    - History is in `QcliMigrations` and every run in `QcliMigrationRuns`.
    - Statuses are `applied`, `pending`, `changed`, `missing` and `failed`. Runs are refused while one is changed or missing, unless `force`.
    - Dry runs show the SQL; `apply_on_startup()` applies pending migrations at startup.
    - Every run publishes the `databases:migration-run` event and writes `migrations.up` / `migrations.down` audit entries.
  - **API** at `/api/v1/qcli/databases` (`databases:read` to read, `databases:migrate` to run migrations):
    - `GET /`, `GET /{db}`, `/{db}/stats`, `/{db}/migrations`, `/{db}/migrations/runs`, `/{db}/migrations/sql`;
    - `POST /{db}/migrations/up`, `/{db}/migrations/down`.
  - **Terminal** `databases` (alias `db`): `list`, `stats`, `migrations`, `up`, `down`.
  - **MCP tools**: `databases_list`, `databases_stats`, `migrations_status|runs|sql|up|down`.
  - **Dashboard page** `@qodalis/cli-dashboard-module-databases`: embedded in the package, served at `/qcli/databases` and registered as a dashboard extension when mounted (`set_dashboard_dir`).
- **Plugin table names** (#83): tables plugins create are `Qcli` + PascalCase with PascalCase columns, the same in the three servers: filesystem-sqlite's `files` is `QcliFiles` (`CreatedAt`, `ParentPath`, index `IX_QcliFiles_ParentPath`), its schema the databases plugin's migrations source `qcli-filesystem`; an existing `files` table is renamed with its data on the first start. Options `table_prefix`, `tables`, `legacy_table_names` (keep the old names). Abstractions: `qcli_table_name(base, CliTableNameOptions, legacy)`, `is_sql_identifier`, `DEFAULT_TABLE_PREFIX`.
- **Plugins declare more when mounted**: `route_permissions` (`CliRoutePermission(path, read, write)`: `read` for GET/HEAD, `write` otherwise, the longest path wins, enforced by the users plugin on a protected server; a path outside the plugin's prefix fails with `invalid route permission: <path>`), `module` (its terminal commands), `permissions` (added to the catalog) and `mcp_providers` (its MCP tools).
- **Dashboard extensions** (cli-server-dashboard `docs/extensions.md`): `builder.add_dashboard_extension(CliDashboardExtension(...))` (or a mapping with the JSON field names), or a mounted plugin's `dashboard_extensions`, adds a page to the admin dashboard, which loads `entry` (an ES module the plugin serves) and mounts it. The admin plugin lists them at `GET /api/v1/qcli/dashboard/extensions` (`{"extensions": [...]}`, registration order, unset optional fields left out, behind the dashboard's sign-in and `admin:access`, `Cache-Control: no-cache`). Invalid entries raise `ValueError` `invalid dashboard extension: <reason>`, a repeated id `dashboard extension already registered: <id>`.

## 1.0.0b29

- The admin plugin embeds dashboard 1.0.0-beta.26: pick a new token's permissions from the server's list, host tokens marked, and a Requests today column when the server counts API usage. The portal plugin embeds `@qodalis/cli-server-portal` 1.0.0-beta.26.
- **Permission catalog.** The server keeps the permissions an API token can be given, each with a description and a group: core registers 11 (`commands:execute`, `shell:execute`, `files:read|write`, `data:query`, `jobs:read|manage`, `logs:read`, `admin:access`, `audit:read|clear`), the users plugin 12 when mounted, and the host its own with `builder.add_permissions([CliPermission(name="rates:read", description="Official exchange rates", group="Rates")])` (plain dicts too; `surface="host"` by default, plugins pass `"cli"`). `builder.permissions` reads it. A malformed entry or a name already registered (case-insensitive) fails startup with `invalid permission entry: <name>` / `permission already registered: <name>`; a host that registers `users:read` makes mounting the users plugin raise. The catalog is for choosing: groups and `cli` tokens accept any permission as before.
- **Users: `GET /api/v1/qcli/permissions`.** The catalog entries the caller holds (`?all=true` with `users:read`: every entry; else `403 permission denied: users:read`), every `cli` entry first, then the host's. The dashboard and the web CLI build their token pickers from it.
- **Users: token `audience`.** Every token is `cli` (the default; existing tokens and stores without the field read as `cli`) or `host`; token views carry `audience`. `POST /tokens` creates `cli` tokens only (`"host"` → `400 host tokens can only be created by the host application`, anything else → `400 audience must be cli or host`). A stored value other than `cli` (or none) counts as host. Custom `IApiTokenStore`s must persist `audience`.
- **Users: host tokens (`users_plugin.api_tokens`).** `ApiTokenManager.create(user_id, ApiTokenCreateOptions(name, expires_in_days, never_expires, permissions, audience, actor))`, `list`, `revoke` and `grantable_permissions(user_id, audience)` let a host application create, list and revoke its users' tokens with the validation, limits, audit entries and events of `POST /tokens`; errors are `UsersApiError` with the route's status and text. Host tokens need permissions that are `host` entries of the catalog (stored in its spelling) and held by the user.
- **Users: host tokens are refused on every CLI route and socket** with `403 forbidden: host tokens cannot use CLI routes` (sockets close 4403 with that text), also with `set_protect_server(False)`, and accepted only by `require_auth` / `require_permission` on the host's routes. CLI routes are the core's, `base_path` and every plugin mounted with `mount_plugin` (`CliServerResult.cli_route_prefixes`, `add_cli_route_prefix(prefix)` for routers included by hand). `users_plugin.require_cli_auth` refuses host tokens for other plugins' routes; the admin API uses it. On an unguarded host route, `get_auth_context(request).is_host_token` tells them apart.
- Users: sockets refused for a pending password change close with 4403 `password change required` (was `forbidden`).
- **Users: the caller of a guarded host route.** `require_auth`, `require_permission` and `require_host_token` set `request.state.qcli_caller`, a frozen `QcliCaller(user_id, user_name, token_id, audience, permissions)`; the returned `AuthContext` has `.caller`.
- **Users: `require_host_token(permission=None)`** accepts only `host` tokens: a session, an adapter user or a `cli` token gets `403 forbidden: a host token is required`, checked before the permission. Its refusals answer `{"error": ...}`.
- **Users: guard tags.** The guards carry `qcli_permission` and `qcli_host_token_only`, read by the public OpenAPI document.
- **Users: token usage.** `users_plugin.set_token_usage_provider(provider)` adds `usage` (`today`, `last30Days`, 30 `days`) to the token views of `GET /tokens` and `api_tokens.list`, last; a failing provider is logged and the views are returned without it. Nothing changes without a provider.
- **Users: guard-only mode** (`set_guard_only()`): no routes, no seeding, no sweeps, no server protection; the guards validate tokens of shared stores. It needs `CliServerOptions(cli_routes=False)` (`ValueError("guard-only needs cli_routes=False")` otherwise; its guards answer 500 until mounted on such a server).
- **Core: `CliServerOptions(cli_routes=False)`** serves none of the CLI's routes and sockets; `mount_plugin` mounts only host-route plugins (`cli_routes = False`) and logs `[qcli] CLI routes are turned off; <prefix> is not mounted` for the others (the admin dashboard is not served either). **`app_docs=False`** turns off FastAPI's `/docs`, `/redoc` and `/openapi.json`. Plugins with `cli_routes = False` are never CLI routes; their `public_paths` are served without sign-in (GET and HEAD), also under `/api/` (`CliServerResult.public_paths`; each under the plugin's own mount and never a CLI route, `invalid public path: <path>` otherwise), and the plugin answers every path under them itself.
- **New plugin: public API** (`qodalis-cli-server-plugin-public-api`). `public_api.guard(permission)` guards the host's public routes: host tokens only, the permission, then per-account quotas from `set_quota(resolver)` (`requests_per_day`, `requests_per_minute`; `403 forbidden: no API access` for 0, `429 quota exceeded: <n> requests per day` / `429 too many requests: <n> per minute` with `Retry-After`) counted over every token of the account (revoked ones included) per UTC day and minute, with `X-RateLimit-Limit/Remaining/Reset` headers; a failing resolver or store answers 500 `internal server error`. Counters live in an `IPublicApiUsageStore` (in memory by default) and feed the token views' `usage` and `public_api.usage.account(user_id)`. The host's public OpenAPI document (a router, an app or a schema) is served at `/qcli/api/openapi.json` with the `qcliToken` bearer scheme, `servers` and `x-required-permission` / `x-host-token-only`, and Swagger UI at `/qcli/api/swagger`, served from a vendored swagger-ui-dist 5.35.1 (nothing loaded from another origin).
- **New plugin: docs** (`qodalis-cli-server-plugin-docs`). Markdown guides from folders or code, with front matter (`title`, `group`, `order`) and variables (`{{baseUrl}}`, `{{name}}`, the host's), served at `/qcli/docs/pages` and `/qcli/docs/pages/<slug>`.
- **New plugin: portal** (`qodalis-cli-server-plugin-portal`). The prebuilt portal site `@qodalis/cli-server-portal` at one mount (`/qcli/portal`, `/` allowed without shadowing the host's routes) with `portal.json` (name, logo, theme, footer links, the docs and API sections).
- The demo serves a public route (`GET /v1/demo/time`, `demo:read`), its OpenAPI document and Swagger UI, guides from `demo/docs` and the portal.

## 1.0.0b28

- The admin plugin embeds dashboard 1.0.0-beta.24: history retention on the job page and the pruning job's last run, collapsible job filters, Jobs and Recent runs tabs, paged lists, tag colours, run controls and row actions as button groups, last-runs tooltips that open the run, a navigation drawer on phones and tablets, shared selects, menus and dialogs, plugin icons, and uploads into folders.
- **Jobs: built-in history retention** (#94). `CliJobsBuilder().set_history_retention(JobHistoryRetention(max_age, max_runs_per_job, failed_max_age, failed_max_runs_per_job, prune_interval))`, off by default; a job overrides them with `CliJobOptions.history` (or keeps everything with `keep_all`). The built-in system job `jobs:prune-history` (group `system`, runs on start when due) deletes the runs past the limits and records how many in its run's `deleted` property. Failed runs are counted on their own when `failed_max_runs_per_job` is set; running runs and each job's newest run are never deleted; runs of jobs no longer registered follow the global limits. Storage providers get an optional `prune_history(limits, default, now)`. Job info carries `historyRetention` (the limits in force, `null` when none apply), for the dashboard.
- **Jobs: run ids in the statistics.** Each recent run in `GET /api/v1/qcli/jobs/stats` carries its execution `id`, so the dashboard's last-runs bars open the run.
- **`get_cli_caller()`**: code a CLI request runs (a processor and the services it calls) reads the authenticated caller (`CliCaller`: `user_id`, `user_name`, `permissions`, `session_id`, `token_id`) for sessions, API tokens (a terminal linked with `server add`) and the host's adapter sign-ins; `None` outside an authenticated CLI request.
- **Icons for the installed plugins.** `/api/v1/qcli/plugins/installed` returns each package's `icon` (one emoji or an image URL, `null` when it has none): `CliInstalledPlugin.icon`, the `__plugin_icon__ = "💳"` of the package's top-level `__init__.py`, or `builder.add_installed_plugin(..., icon="💳")`. Every package has one: Core 🖥️, Abstractions 🧱, Admin 🎛️, Audit 🛡️, Aws ☁️, Jobs ⏱️, Users 👥, Weather 🌤️, Data Explorer 📊, Filesystem 📁 (Json 📄, Sqlite 🪶, S3 🪣) and the data explorer providers (Sql 🪶, Postgres 🐘, Mysql 🐬, Mssql 🗃️, Mongo 🍃, Redis 🔴, Elasticsearch 🔍). The weather and aws modules have icons too (`ICliModule.icon`: 🌤️, ☁️).

## 1.0.0b27

- The admin plugin embeds dashboard 1.0.0-beta.23: built with Qodalis CLI 2.5.2; New token dialog, SQL editor with completion, the query as run ("Ran as"), module icons on the Plugins page, and the return to the dashboard after an external sign-in.

- **API tokens and terminal links with an external auth adapter (opt-in).** `CliUsersBuilder.set_allow_api_tokens()` (`AuthServiceOptions.api_tokens_with_external_auth`): a host that signs users in itself (`set_auth_adapter`) can also accept the plugin's API tokens. `qcli_pat_` bearer tokens are checked before the adapter, users signed in by the host create tokens (`POST /tokens`) and approve terminal links (`POST /link/code/{code}/approve`); both answered 501 before and still do without the option. The token's user comes from the user store by id (it must find the adapter's users and report `disabled`); keep tokens in a persistent `IApiTokenStore`.
- Admin: `set_sign_in_url()` / `AdminConfig.sign_in_url` documents how the dashboard (1.0.0-beta.23) sends users back after the host's sign-in: `{returnTo}` in the URL, or a `returnTo=` parameter, to open again with `#token=`.
- **Data explorer: names with capitals just work.** When a read-only SQL source answers that a table or column does not exist because the query wrote it without quotes (PostgreSQL folds such names to lower case: `SELECT count(*) FROM Users` looks for `users`), the query runs again with the name as the schema spells it, quoted the source's way: `"Users"` (PostgreSQL, SQLite), `[Users]` (SQL Server with a case-sensitive collation), `` `Users` `` (MySQL on a case-sensitive file system). The result's new `executed_query` (`executedQuery` in JSON) shows the query that ran, and the audit entry keeps both. Only a name that matches exactly one table the source shows or column it does not drop is changed, never inside strings or comments, nor a function name (`count(`). Off with `DataExplorerProviderOptions(fix_name_case=False)`; writable sources (`read_only=False`) are never run twice. Providers quote with `IDataExplorerProvider.quote_name` (default ANSI double quotes). When nothing can be fixed, the error adds the hint `Names with capital letters need double quotes: "Users"`.
- **Command modules on the Plugins page for commands registered on their own.** Commands added with `builder.add_processor(...)` are listed under the module their `module` attribute names (ignoring case), and the page's switch disables them too; before, only `add_module` modules were listed. A module with the same name and no processors describes the group (description, version, icon); without one, the group takes its version and description from the commands' package. `ICliModule.icon` (one emoji or an image URL, default `None`) is returned as `icon` by `GET /api/v1/qcli/plugins` and the new `GET /api/v1/qcli/plugins/{id}` for the dashboard's module cards. `CliBuilder.processors` lists every registered command processor.

## 1.0.0b26

- The admin plugin embeds dashboard 1.0.0-beta.22: built with Qodalis CLI 2.5.1 (touch scrolling of the terminal page on phones, page no longer scrolls under the panel).

- Fix: data explorer sources with `AllowedObjects` or `MaskColumns` refused ordinary queries on quoted names, such as PostgreSQL's mixed-case tables: `SELECT "Id" FROM "Users" WHERE ...` answered `'WHERE' is not available on this source` (and `FROM "Users" u` named `u`). Under the MySQL reading, where `"..."` is a string, the string was dropped, so the next word was taken as the table. It now stays one name token, so the table is checked by its name, and a quoted masked column in `WHERE` is still refused.

## 1.0.0b25

- The admin plugin embeds dashboard 1.0.0-beta.20: job tags, command metadata, the terminal link page, API tokens, a data explorer page and audit details.

- **Jobs: tags.** `tags` on a job (`CliJobOptions.tags`: free-form labels such as `country:ro`, `bank`; each 1-64
  characters without commas, at most 20, duplicates ignoring case dropped), always present in job info (`[]` when
  none). `GET /api/v1/qcli/jobs?tag=a&tag=b` (repeatable or comma-separated) lists the jobs that have every tag,
  ignoring case; `/executions` and `/stats` take `tag=` too. `PUT /jobs/{id}` accepts `tags` (replaces them, `null`
  clears, invalid ones are 400 `INVALID_VALUE`). MCP `jobs_list` takes `tags` and shows them. Same on the .NET and
  Node servers.
- **Commands as rich as web commands.** Processors may define `aliases`, `examples` (`CliCommandExample`),
  `documentation`, `module` and `icon`, and parameters `allowed_values` and `examples` (example values); `GET /api/v1/qcli/commands` sends them
  (absent when unset), so the web CLI's `help` shows examples and documentation, groups and decorates the command, and
  completes and checks allowed values. The server resolves aliases of commands and sub-commands for `/execute`,
  streaming and permission checks (a command name wins over another command's alias). MCP command tools list allowed
  values as an `enum` (and parameter examples as `examples`) and refuse other values. The built-in processors and the weather plugin carry examples and
  documentation (`hash --algorithm` its algorithms). Same on the .NET and Node servers (spec: web-cli
  `docs/superpowers/specs/2026-11-12-server-command-metadata-design.md`).
- **Data explorer: per-source permissions, read-only mode, masking, audit and limits**, the same on the .NET and Node servers
  (spec: web-cli `docs/superpowers/specs/2026-11-12-data-explorer-hardening-design.md`):
  - **Breaking:** sources are read-only by default. Writes, DDL and several statements are refused before the provider runs
    (`errorCode: "read_only"`), and the SQL providers run queries in a read-only transaction (SQLite `PRAGMA query_only`,
    Postgres `BEGIN READ ONLY`, MySQL `START TRANSACTION READ ONLY`, SQL Server a transaction that is always rolled back).
    Set `read_only=False` on a source that must write.
    The SQL check reads quotes and comments as MySQL (`--` before a space, `#`, `/*! ... */`, `"..."` strings), Postgres
    (`E'...'`, `$$...$$`, nested comments), SQL Server (nested comments, `[...]`) and SQLite each do, and a query must
    pass under every reading, so a quote or comment one database skips cannot hide a statement another runs.
    `COMMIT`, `ROLLBACK` and `DENY` count as writes (SQL Server runs statements without `;`), a lone carriage return is
    refused, and names may hold non-ASCII letters.
  - `allowed_objects` reads every item of a `FROM`/`USING` list, CTE names only within their scope, `db..table`, and the
    table after a leading `DESCRIBE`/`EXPLAIN`; MongoDB `$lookup`, `$graphLookup`, `$unionWith`, `$out` and `$merge`
    (with `\uXXXX` escapes decoded); Elasticsearch paths cut at `?`/`#` and index names in the body; Redis keys split as
    the provider splits them. The same rules as the Node and .NET servers.
  - On SQL sources with `mask_columns` or `drop_columns`, those columns can only be selected by their own name (a plain
    item of the outer select list, or `*`): aliases, expressions, filters, sorts, subqueries, set operations, Postgres
    whole-row values and `U&"..."` names are refused before the query runs (`errorCode: "masked_column"`, `Masked column
    'email' can only be selected by its own name`), and a result that returns such a column under another name is withheld.
  - Masking also applies inside JSON text (a JSON column, an Elasticsearch answer): it is masked like a document.
  - The Postgres provider runs queries only as prepared statements (one statement each, also when they return no rows).
  - The schema's columns use camelCase keys (`primaryKey`) over HTTP and in MCP `data_schema`, as on the other servers.
  - New `DataExplorerProviderOptions` fields: `required_permissions` (403, and the source is hidden from `/sources` and MCP
    `data_sources`), `allowed_objects` (globs of tables, collections, keys or indices), `mask_columns` (`***`), `drop_columns`,
    `audit` (default `True`: a `data_query` entry in the audit log per execute), `rate_limit=DataExplorerRateLimit(max_queries,
    window_seconds)` (429) and `export_permissions`.
  - `GET /sources` adds `readOnly` and `canExport` (and no longer lists the source's policy options); the schema lists only
    allowed tables without dropped columns; results carry `errorCode` when refused. The MCP `data_*` tools apply the same rules.
  - `DataExplorerExecutor(registry, audit=..., rate_limiter=...)`, `execute_async(request, caller=None)`,
    `create_data_explorer_router(registry, executor, caller=...)`; new abstractions `DataExplorerCaller`,
    `DataExplorerAuditEntry`, `DataExplorerRateLimit`, `is_read_only_source`; providers that only read can set
    `read_only_queries = True`. The plugins read the new options with `getattr`, so they still work with older abstractions.
- **Link a terminal through the browser** (users plugin), the same on the .NET and Node servers
  (spec: web-cli `docs/superpowers/specs/2026-11-12-server-linking-design.md`): `POST /api/v1/qcli/link` (public,
  rate limited) returns a code and the approval page, `GET /link/{linkId}` hands the terminal its API token once the
  user approved at `/qcli/admin/link?code=…` (`GET /link/code/{code}`, `POST …/approve`, `POST …/deny`). The token is
  the approver's, named `link <client>`, scoped to the requested permissions. `set_link_approval_url` points to a
  host's own page; `link_approved` and `link_denied` are audited. A request is approved once (a parallel approval gets
  404); one that expires while its token is created gets 404 and the token is revoked.

## 1.0.0b24

- The admin plugin embeds dashboard 1.0.0-beta.19: a phone layout (below 768 px the sidebar is a drawer, stat cards wrap, panels stack and tables scroll in their container) and tests for the live log tail.
- **Admin: live log entries.** Every log line the admin plugin captures is published as `log:entry` (`timestamp`,
  `level`, `message`, `source`, audience `logs:read`) on the server's event bus once the plugin is mounted (through
  `AdminBuildDeps.broadcast_fn` when set; `AdminBuildDeps.event_bus` names a bus explicitly), so the dashboard's Logs
  page follows the log live. The buffer stored its `broadcast_fn` but never called it, so new entries showed only after
  a refresh. A line logged while an entry is delivered is kept but not published; lines from worker threads are
  published on the loop seen before. `qodalis-cli-server-plugin-admin` now depends on
  `qodalis-cli-server-abstractions`.

## 1.0.0b23

- The admin plugin embeds dashboard 1.0.0-beta.18: pages update live from the server events (jobs, job detail, files, audit, overview, events), a run's full error in a details dialog, max concurrent runs in the job editor, and no session token in WebSocket URLs when the ticket request fails.

- **Server events**, the same design on the .NET, Node and Python servers:
  - `create_cli_server` creates an event bus, `CliServerResult.event_bus` (`CliEventBus`; `ICliEventBus`, `CliEvent`,
    `CliEventAudience` and `cli_event_pattern_matches` in `qodalis_cli_server_abstractions.events`).
    `publish(type, data=None, audience=None)` and `subscribe(pattern, handler)` (`*`, `ns:*` or a type; returns the
    unsubscribe function). Handler errors are logged and never fail the publisher.
  - Every event is one flat camelCase JSON object on `/ws/v1/qcli/events` (`type`, `timestamp` in ISO 8601 UTC, the
    fields) and has an audience: everyone, a permission, a user, or the user or the permission. Sockets get only the
    events of their audience; on a server without authentication every socket gets every event. Each socket has its own
    send queue, so publishing (and the request that published) never waits on a slow client.
    `broadcast_message` keeps working.
  - New events: `job:triggered`, `job:updated`, `job:runs-deleted`; `user:*`, `group:*`, `token:*`, `session:*`
    (users plugin); `fs:changed` (filesystem API and MCP `fs_*` tools); `command:executed` (REST execute, stream and MCP;
    the processor name only); `audit:recorded` (audit plugin). `job:paused`, `job:resumed` and `job:stopped` carry
    `status`. See the README, *Server Events*.
  - Jobs events have the audience `jobs:read`. The jobs and users plugins publish on the server's bus once mounted;
    their `build(broadcast_fn=...)` keeps working without a bus and takes `event_bus=`.
  - `CliAuditPlugin.log(entry)` stores an entry and publishes `audit:recorded`.
- **Jobs: max concurrent runs, and no job started twice** (jobs plugin), the same design on every server:
  - New option `max_concurrent_runs` (`CliJobOptions`, default 1; `PUT /jobs/{id}` `maxConcurrentRuns`, an integer
    >= 1, else 400 `INVALID_VALUE`). A run holds its slot from start to end, retries and retry delays included. When every slot is
    taken, `overlap_policy` decides: `skip` drops the due run (a trigger answers 409), `queue` keeps one run waiting
    (coalesced; a trigger answers `{"message": "Job queued", "queued": true}`), `cancel` cancels the oldest run and
    starts the new one once that run has ended (queued meanwhile; further due runs merge into it).
  - Job info adds `maxConcurrentRuns`, `runningExecutionIds` (oldest first) and `queued`; `currentExecutionId` is the
    newest running run. `cancel` cancels every running run and drops a queued one.
  - Fixes: run on start skips a job that ran recently (interval not yet passed, no missed cron occurrence); a retry
    delay no longer frees the job; `queue` no longer builds a backlog and `cancel` no longer runs two "current"
    executions; due runs at the same moment can no longer both start; a cron timer that fires early no longer runs the
    occurrence twice; runs left `running` by a previous process are finished as `cancelled` (`Interrupted: the server
    stopped during the run`) when the scheduler starts.
  - A run of a job with a timeout that is cancelled by hand (cancel, stop, overlap `cancel`) is recorded as
    `cancelled` with `job:cancelled`, also when the job ignores its cancellation until the timeout; only a real timeout
    is `timed_out`.
  - `JobRegistration` has `runs`, `queued` and `running_execution_ids`; `current_execution_id` and
    `current_cancellation` are read-only properties now, and the `queue` field is gone. `trigger()` returns `"started"`
    or `"queued"`.

## 1.0.0b22

- The admin plugin embeds dashboard 1.0.0-beta.17: logs jump to newest, pause, copy an entry and collapsed exceptions; jobs run again and delete runs from the recent runs table.

## 1.0.0b21

- The admin plugin embeds dashboard 1.0.0-beta.16: delete job runs (one, a result's or every finished one), older log
  entries load when scrolling up with stable paging, and themed dropdown arrows.
- **Delete job runs** (jobs plugin), the same API on the .NET, Node and Python servers:
  - `DELETE /api/v1/qcli/jobs/{id}/history/{exec_id}` deletes one run and its logs: `200 { "message": "Run deleted" }`,
    `404` `JOB_NOT_FOUND` / `EXECUTION_NOT_FOUND` (missing, or a run of another job), `409 EXECUTION_RUNNING`, `501
    NOT_SUPPORTED` when the storage cannot delete runs.
  - `DELETE /api/v1/qcli/jobs/{id}/history?status=` deletes every finished run of the job, or only those with that
    status; running runs are kept. Answers `200 { "deleted": n }`, with the same 404 and 501 answers.
  - `CliJobScheduler.delete_execution(job_id, exec_id)` and `delete_executions(job_id, status=None)` (also on
    `ICliJobScheduler`); they delete through the storage's `delete_execution`, and a storage that keeps the base
    implementation gets `NotSupportedError` (501).
  - MCP tool `jobs_delete_runs {id, execution?, status?}` (`jobs:manage`, `destructiveHint`).

## 1.0.0b20

- Version only, to keep every server package on one version: the .NET jobs EF Core store fix of this release has no Node/Python counterpart.

## 1.0.0b19

- The admin plugin embeds dashboard 1.0.0-beta.15: charts on the Overview, Jobs, Logs, Audit and Commands pages, filters kept in the URL, sortable tables and a Ctrl+K command palette, using the endpoints below.

Read-only endpoints for the admin dashboard's charts, the same JSON on the .NET, Node and Python servers. Times are ISO
8601 UTC (`2026-11-05T21:00:00.000Z`), durations milliseconds; bad times or numbers answer `400 { "error": "..." }`.

- **Jobs** (jobs plugin): `GET /api/v1/qcli/jobs/stats?since=&until=&buckets=&job=&group=` counts every run started in
  the range (default the last 24 hours; `buckets` 1..200, default 24) per time column by outcome, with totals and per-job
  `successRate`, `avgDuration`, `p95Duration`, `maxDuration`, `lastFailureAt` and the 12 newest runs. It pages through
  the storage's execution query, so it works with every storage provider.
- **Log histogram** (admin plugin): `GET /api/v1/qcli/logs/histogram?from=&to=&buckets=&level=&search=&service=` counts
  the buffered log entries per level (`DEBUG`, `INFO`, `WARN`, `ERROR`) and time column. `GET /api/v1/qcli/logs` now
  takes several levels (comma-separated), searches the source too and takes `service` (the in-memory buffer has no
  services, so a service matches nothing) and `from`/`to`; it answers `hasMore`, `store: "memory"`, `persistent: false`
  and `services: []` like the .NET server, so the dashboard filters on the server.
- **Status history** (admin plugin): `GET /api/v1/qcli/status/history?minutes=` (default 60, 1..1440). The plugin
  samples memory, CPU (process CPU time over wall time, % of one core), WebSocket clients and shell sessions every 15
  seconds and keeps 24 hours in memory; the sampler is an asyncio task started with the app's lifespan and cancelled on
  shutdown.
- **Command usage** (server): `GET /api/v1/qcli/commands/stats` lists executions, failures (exit code != 0 or an
  exception), average duration and the last execution and failure per root command since the server started, counted
  for `/execute`, `/execute/stream` and the MCP tools (`known: false` for unknown commands). The executor's `usage`
  (`CliCommandUsageTracker`) holds the counters.
- **Audit** (audit plugin): `GET /api/v1/qcli/audit` takes `from` and `to` (ISO 8601 or epoch milliseconds,
  `from <= timestamp < to`). `AuditQueryOptions` gains `from_` and `to` (Unix time in ms); `InMemoryAuditStore` filters
  by them. A store of your own should filter by them too.

## 1.0.0b18

Every package moves to the version of the .NET server's release, which adds a Seq logs plugin
(`Qodalis.Cli.Plugin.Logs.Seq`) and new filters on `GET /api/v1/qcli/logs` (`service`, `from`, `to`, several levels).
This server's logs API keeps its current filters.

- The admin plugin ships dashboard 1.0.0-beta.14. Its Logs page shows this server's logs oldest first, so older pages
  load on top and live entries arrive at the bottom.

## 1.0.0b17

### Audit plugin

- The audit log is a plugin of its own, **qodalis-cli-server-plugin-audit** (`qodalis_cli_audit`,
  `result.mount_plugin(CliAuditBuilder().build())`): the entries, the in-memory store and `GET/DELETE
  /api/v1/qcli/audit`, listed on the dashboard's Plugins page and shown as its Audit page. The users plugin depends on
  it and mounts it, so existing hosts keep their audit log; a host can use it without the users plugin. A server serves
  one audit log: mounting the audit plugin again, or a second one, adds no routes (the new `before_mount(server)` hook
  of `mount_plugin`). See [its README](plugins/audit/README.md).
- The contract moves to **qodalis-cli-server-abstractions** (`qodalis_cli_server_abstractions.audit`: `AuditEntry`,
  `AuditMetadata`, `AuditQueryOptions`, `IAuditStore`), so a store of your own needs only the abstractions.
  `InMemoryAuditStore` is in `qodalis_cli_audit`. `qodalis_cli_users` (and its `models`, `stores` and
  `stores.in_memory_audit_store` modules) still re-exports `AuditEntry`, `AuditQueryOptions`, `IAuditStore` and
  `InMemoryAuditStore`, so existing imports keep working. **Breaking**: `qodalis_cli_users.controllers` no longer has
  `create_audit_router` (use `qodalis_cli_audit.create_audit_router`), and the users plugin's `router` no longer serves
  `/audit`: mount the users plugin with `result.mount_plugin(...)` (which mounts the audit plugin), or include
  `users_plugin.audit_plugin.router` yourself.
- `CliAuditBuilder().set_store(store)` keeps the log elsewhere; the users plugin's `set_audit_store(store)` still works
  and does the same. A store set on either side is kept, whichever plugin is mounted first; of two set stores the one
  mounted last. `users_plugin.audit_plugin` is the audit plugin, `users_plugin.audit_store` its store.
- The server protection's route policy protects the audit API itself (`audit:read` for `GET`, `audit:clear` for
  `DELETE`); without server protection the users plugin still guards it. Clearing records `audit_cleared` with the
  caller (`userId`, `actorId`, `remoteAddress`). A `limit` or `offset` that is not an integer >= 0 answers `400
  {error}`; `limit=0` returns no entries (it returned up to the whole log).
- The admin status reports the `audit` feature when the audit plugin is installed.
- There is no database audit store in this server (the .NET server's Audit.EfCore has no Python counterpart):
  implement `IAuditStore` for one.

### Admin

- The admin plugin ships dashboard 1.0.0-beta.13: the Terminal page is shown on every server again (it was hidden
  without an OS shell).

## 1.0.0b15

### Installed plugins

- The admin plugin lists the installed server plugin packages at `GET /api/v1/qcli/plugins/installed` (`id`, `name`,
  `version`, `description`), shown on the dashboard's Plugins page (dashboard 1.0.0-beta.12). Mounted plugins
  (`mount_plugin`), filesystem and data-explorer providers list themselves; a package of your own calls
  `builder.add_installed_plugin(...)`.
- The admin status reports the `audit` feature when the users plugin is installed (the dashboard's Audit page), and
  `jobs` when the jobs plugin is mounted.

### Audit log

- Audit entries gain `target` (what the event is about when it is not a user; the group events set `group:<name>`)
  and `properties` (the application's own values); `AuthService.audit(..., target=, properties=)`.
- `GET /audit` and `AuditQueryOptions` gain `actor` and `offset`. Without a `limit` the newest 100 entries are
  returned (all before), at most 1000.
- `IAuditStore.prune(before)` deletes old entries (the default keeps everything; the in-memory store implements it).

### Jobs

- Stable job ids: `CliJobOptions.id`, by default the name (`name-2`, ... for duplicates), so saved pauses and history
  find their job again after a restart. Ids may not contain `/`, `\`, `?` or `#`; a default id replaces them.
- A saved pause or stop is kept across restarts, but `enabled=False` in the code always keeps a job stopped.
- Interval jobs keep their rhythm across restarts and pauses (next run = last run + interval); `run_on_start` runs a
  job once right after the scheduler starts. The scheduler saves the last run after every run.
- Inside a job: `context.properties` (saved with the run and returned by the API and the `jobs_history` MCP tool) and
  `context.discard()` (leave a completed run that found nothing to do out of the history). Implementers of
  `ICliJobExecutionContext` must add them. A run whose properties cannot be saved is saved without them.
- `GET /api/v1/qcli/jobs/executions?job=&group=&status=&since=&limit=&offset=`: runs across jobs, newest first;
  `JobExecutionQuery`, `ICliJobStorageProvider.query_executions`, `prune(before)` and `delete_execution(id)` (with
  defaults), and `CliJobScheduler.query_executions`.
- `ICliJobScheduler` (a Protocol) for the host's own code; `CliJobScheduler.jobs` is read-only and `JobRegistration`
  public.

### Other packages

- Released at `1.0.0b15` with the .NET and Node servers; the admin plugin ships dashboard 1.0.0-beta.12.

## 1.0.0b14

### Dashboard name and icon

- `CliAdminBuilder.set_branding({"name", "icon_url", "cli_name"})`: the name and icon an application ships the
  dashboard under, and its terminal's name, served in `/qcli/admin/theme.json` and validated when set. Defaults:
  Qodalis Cli Admin with the Qodalis Solutions logo. See [Name and icon](plugins/admin/README.md#name-and-icon).

### Other packages

- Released at `1.0.0b14` with the .NET and Node servers; no other change.
