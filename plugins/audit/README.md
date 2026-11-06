# qodalis-cli-server-plugin-audit

The audit log of the [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python): who did
what, to whom or what, and when. It keeps the entries (`IAuditStore`), serves them at `/api/v1/qcli/audit` and is the
admin dashboard's **Audit** page and the CLI's `audit` command. The [users plugin](../users/README.md) mounts it and
records sign-ins, failed sign-ins, user, group, token and shell events; an application records its own changes in the
same log.

## Install

```bash
pip install qodalis-cli-server-plugin-audit
```

The users plugin depends on it, so installing `qodalis-cli-server-plugin-users` installs it too.

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_audit import CliAuditBuilder
from qodalis_cli_users import CliUsersBuilder

result = create_cli_server(...)

result.mount_plugin(CliUsersBuilder().build())   # mounts the audit plugin too
# or on its own, without the users plugin:
result.mount_plugin(CliAuditBuilder().build())
```

A server serves one audit log. Mounting the audit plugin again, or a second audit plugin, adds no routes: the plugins
share one store, the one set on either of them (`CliAuditBuilder().set_store(...)` or the users plugin's
`set_audit_store(...)`), else the default in-memory store; of two set stores the one mounted last is kept.
`get_audit_plugin(result)` returns the audit plugin serving a server.

## Recording your own changes

Log an entry with the plugin (`AuditEntry`, `qodalis_cli_server_abstractions.audit`):

```python
from qodalis_cli_server_abstractions.audit import AuditEntry

await audit_plugin.log(AuditEntry(
    event="role.save",
    actor=current_user.email,
    target="role:editor",
    properties={"permissions": ["jobs.view"]},
))
```

An entry has the `event`, the user it is about (`username`), who did it (`actor`), what else it is about (`target`,
e.g. `group:admins`), `details`, `metadata` (`AuditMetadata`: `remoteAddress`, `sessionId`) and the application's own
values (`properties`). With the users plugin, `users_plugin.audit_store` is the same store.

`audit_plugin.log(entry)` stores the entry and, once the plugin is mounted, publishes `audit:recorded` on the server's
event bus (audience `audit:read`): the entry's fields as `GET /audit` returns them, with the entry's time as the event's
`timestamp` (ISO 8601). The users plugin's entries and `audit_cleared` are published the same way. Writing to
`audit_plugin.store` directly records the entry without an event.

## API

| Endpoint | Permission | Description |
|---|---|---|
| `GET /api/v1/qcli/audit?event=&username=&actor=&from=&to=&limit=&offset=` | `audit:read` | Entries newest first; filters are exact and case-insensitive; `from` (inclusive) and `to` (exclusive) are ISO 8601 times (no offset = UTC) or epoch milliseconds; 100 entries without a `limit`, at most 1000 (`DEFAULT_AUDIT_LIMIT`, `MAX_AUDIT_LIMIT`) |
| `DELETE /api/v1/qcli/audit` | `audit:clear` | Clears the log, then records `audit_cleared` with who did it (204) |

A `from` or `to` that is not a time, or a `limit` or `offset` that is not an integer >= 0, answers `400 { "error": "..." }`. The permissions apply with the users
plugin: its server protection checks them (route policy), and without server protection (`set_protect_server(False)`)
the users plugin still guards the audit API. On a server without an auth plugin the API is open, like the jobs API.

## Storage

The default store (`InMemoryAuditStore`) keeps the latest 1000 entries in memory (failed sign-ins apart, at most 200, so
a flood of them keeps the rest). Keep the log elsewhere by implementing `IAuditStore`
(`qodalis_cli_server_abstractions.audit`: `log`, `get_entries`, `clear`, optionally `prune`) and setting it:

```python
audit_plugin = CliAuditBuilder().set_store(MyAuditStore()).build()
```

`IAuditStore.prune(before)` (Unix time in ms) deletes old entries; run it on a schedule for a store that keeps
everything (the default implementation keeps everything and returns 0).

## License

MIT
