# Change Log

Notable changes to the Python CLI server packages (`qodalis-cli-server`, `qodalis-cli-server-abstractions`,
`qodalis-cli-server-plugin-*`). Every package shares one version with the .NET and Node servers (`1.0.0b19` is
PEP 440 for `1.0.0-beta.19`).

## 1.0.0b19

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
