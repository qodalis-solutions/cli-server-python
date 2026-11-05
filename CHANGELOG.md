# Change Log

Notable changes to the Python CLI server packages (`qodalis-cli-server`, `qodalis-cli-server-abstractions`,
`qodalis-cli-server-plugin-*`). Every package shares one version with the .NET and Node servers (`1.0.0b15` is
PEP 440 for `1.0.0-beta.15`).

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
