# qodalis-cli-server-plugin-docs

Markdown guides for the host application of a [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python),
from folders or code, served as JSON. The [portal plugin](../portal/README.md) shows them next to the API reference of
the [public-api plugin](../public-api/README.md); the plugin also works alone.

## Install

```bash
pip install qodalis-cli-server-plugin-docs
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_docs import CliDocsBuilder

result = create_cli_server()
docs = (
    CliDocsBuilder()
    .set_name("RateBell API")
    .set_variable("support", "help@ratebell.app")
    .add_folder("docs")                                   # relative to the working directory
    .add_page("errors", "# Errors\nEvery error is {\"error\": \"...\"}.")
    .build()
)
result.mount_plugin(docs)
```

## Routes

| Route | Answer |
|---|---|
| `GET <base>/pages` | 200, `Cache-Control: no-cache`: `[{"slug", "title", "group", "order"}]` in page order |
| `GET <base>/pages/<slug>` | 200, `Cache-Control: no-cache`: `{"slug", "title", "group", "order", "markdown"}` |
| unknown or invalid slug | 404 `{"error": "page not found"}` |

`HEAD` is answered too. No sign-in; credentials are ignored, also under `/api/` on a server protected by the users
plugin (`<base>/pages` and everything under it are the plugin's `public_paths`, answered by the plugin itself; a base
under a CLI route fails `mount_plugin`). Under an ASGI `root_path` `{{baseUrl}}` includes it; a `root_path` starting
with `//` or `/\` answers 400 `{"error": "invalid path base"}`. Page order: `order` ascending, then `title` (case-insensitive), then
`slug`. The plugin's routes are host routes (`cli_routes = False`).

## Pages

- **Folders** (`add_folder(path)`): every `*.md` file directly in the folder (no subfolders; names starting with `.` or
  `_` are ignored; symbolic links and files over 1 MB are skipped and logged), read once by `build()`. The slug is the file name without `.md`, lower-cased; it must match
  `^[a-z0-9][a-z0-9-]*$`. `index` is the guides' home page.
- **Code** (`add_page(slug, markdown)`): same slug rule, same front matter.
- **Front matter**: only when the first line is exactly `---`, up to the next line that is exactly `---` (without
  that line the whole file is the body). Each line is `key: value`; `title`, `group` and `order` are read, other keys
  ignored; a value in matching `"…"` or `'…'` is unwrapped; `order` is an integer. The front matter is removed from
  `markdown`.
- **Defaults**: `title` is the first body line starting with `# ` (else the slug), `group` is `Guides`, `order` is `0`.

```markdown
---
title: Authentication
group: Basics
order: 2
---
Send `Authorization: Bearer <token>` to {{ baseUrl }}/v1/rates. Questions: {{support}}.
```

## Variables

`{{ key }}` in titles and markdown is replaced per request; unknown keys are left as written.

| Variable | Value |
|---|---|
| `baseUrl` | `set_base_url(url)`, else the request's `scheme://host` and root path (no trailing `/`) |
| `name` | `set_name(name)`, default `Documentation` |
| any other | `set_variable(key, value)`; keys match `^[A-Za-z][A-Za-z0-9_.-]*$` |

## Options

| Builder method | Default | |
|---|---|---|
| `set_base_path(path)` | `/qcli/docs` | starts with `/`, no trailing `/`, not `/` |
| `set_title(title)` | `Guides` | the portal's label of the guides |
| `set_name(name)` | `Documentation` | `{{name}}` |
| `set_base_url(url)` | the request's URL | `{{baseUrl}}` |
| `set_variable(key, value)` | — | host variables |
| `add_folder(path)`, `add_page(slug, markdown)` | — | the pages |

`build()` raises `ValueError` with one of: `invalid docs base path: <path>`, `docs folder not found: <path>`,
`invalid docs page name: <file name>`, `invalid docs page slug: <slug>`, `duplicate docs page: <slug>`,
`invalid order in docs page: <slug>`, `invalid docs variable: <key>`, `docs variable is built in: <key>`.

The plugin's `pages` holds the parsed pages (`DocsPage(slug, title, group, order, markdown)`, variables not
replaced) and `portal_section` (`{"kind": "docs", "title", "base_path"}`) what the portal plugin shows.

## Terminal commands

`docs`, registered by `mount_plugin` (the plugin's `module`). The pages are public, so the command needs no permission
beyond `commands:execute` on a protected server. `{{baseUrl}}` is the `set_base_url` URL, else empty (links relative to
the server root): a command has no request to take the host from.

| Command | Permission | Does |
|---|---|---|
| `docs`, `docs list [--group <group>] [--json]` | none | Slug and title of every page, by group, in page order (`GET /pages`) |
| `docs read <slug> [--json]` | none | The page's markdown with the variables replaced, its title as a heading unless the page starts with one (`GET /pages/{slug}`) |

```text
$ docs
Guides
  getting-started  Getting started with RateBell API
  auth             Authentication

Reference
  errors           Errors

$ docs read auth
# Authentication

Send `Authorization: Bearer <token>` with every request.
```

## Install from the dashboard

Installable at runtime from the server marketplace (the dashboard's Plugins → Marketplace, or
`admin plugins install docs`; it loads at the next start), or in code through the same entry, its options the
defaults of the settings below (a saved setting overrides them):

```python
from qodalis_cli_docs import plugin as docs

builder.add_plugin(docs, {...})
```

Entry point `docs = "qodalis_cli_docs.plugin:create_plugin"` (group `qodalis_cli.plugins`), load order 50. Its settings
(provider `docs`, section `settings`; `config set docs.settings.<key> <value>`, then a restart):

| Setting | Type | Default | |
|---|---|---|---|
| `folder` | string | `"pages"` | The Markdown files (relative paths from the plugin's data directory) |
| `basePath` | string | `"/qcli/docs"` | Where the guides are served |
| `title` | string | `"Guides"` | Title |
| `name` | string | `"Documentation"` | Name |

## License

MIT
