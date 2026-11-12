# Change Log

Notable changes to the Python CLI server packages (`qodalis-cli-server`, `qodalis-cli-server-abstractions`,
`qodalis-cli-server-plugin-*`). Every package shares one version with the .NET and Node servers (`1.0.0b19` is
PEP 440 for `1.0.0-beta.19`).

## Unreleased

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
