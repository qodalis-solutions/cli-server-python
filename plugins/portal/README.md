# qodalis-cli-server-plugin-portal

The portal of a [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python) host: the
prebuilt site `@qodalis/cli-server-portal` (guides and API reference, light and dark, code samples and a "try it" box)
at one mount, with `portal.json`, which tells the site what to show. It shows the guides of the
[docs plugin](../docs/README.md) and the API reference of the [public-api plugin](../public-api/README.md); each is
optional ("Nothing published yet" without either).

## Install

```bash
pip install qodalis-cli-server-plugin-portal
```

## Quick Start

```python
from qodalis_cli_docs import CliDocsBuilder
from qodalis_cli_portal import CliPortalBuilder
from qodalis_cli_public_api import CliPublicApiBuilder

docs = CliDocsBuilder().set_name("RateBell API").add_folder("docs").build()
public_api = CliPublicApiBuilder().set_users(users_plugin).set_openapi(public_router).build()
portal = (
    CliPortalBuilder()
    .set_path("/")                                   # RateBell serves its docs at the root
    .set_name("RateBell API")
    .set_logo_url("/static/logo.svg")
    .set_theme({"mode": "system", "light": {"primary": "#0a7"}})
    .add_footer_link("Terms", "https://ratebell.app/terms")
    .add_section(docs)
    .add_section(public_api)
    .build()
)
for plugin in (docs, public_api, portal):
    result.mount_plugin(plugin)
```

## Routes

The portal answers exactly these paths under its mount, GET and HEAD only; anything else (other paths, other methods,
a missing asset) falls through to the host's routes and the framework's 404, so a portal at `/` never shadows a host
route such as `GET /v1/rates` or `GET /health`, whatever the order they were added in.

| Path (relative to the mount) | Answer |
|---|---|
| the mount itself, `/`, `/guides/<slug>`, `/reference`, `/reference/<tag>` | 200 `text/html; charset=utf-8`, `Cache-Control: no-cache`: the site's `index.html` with `<base href="<mount>/" />` |
| `/portal.json` | 200 JSON, `Cache-Control: no-cache` |
| `/assets/<file>` | the site's file (`Cache-Control: public, max-age=31536000, immutable`), else not handled |
| `/favicon.svg` | the site's file, else not handled |

The pages are served without sign-in (GET and HEAD), also under `/api/` on a server protected by the users plugin (the
portal's `public_paths`: the mount, `portal.json`, `favicon.svg`, and everything under `assets/`, `guides/` and
`reference/`; CLI routes always keep their sign-in). Away from `/`, the portal answers every path under those prefixes
itself (404 `{"error": "not found"}` for what it does not serve), so a path served without sign-in never reaches a host route; at `/` (never
under `/api/`) a missing asset falls through to the host instead. A mount that holds CLI routes (`/api`, `/ws`,
`/api/v1`, anything under `/api/qcli`, ..., in any letter case) is refused. Under an ASGI `root_path`
(an app mounted under a prefix) the `<base href>` and the URLs in `portal.json` start with it; a `root_path` starting
with `//` or `/\` answers 400 `{"error": "invalid path base"}`.

```json
{
  "name": "RateBell API",
  "logo": null,
  "theme": { "mode": "system", "dark": {}, "light": { "primary": "#0a7" } },
  "footerLinks": [ { "label": "Terms", "url": "https://ratebell.app/terms" } ],
  "docs": { "title": "Guides", "pages": "/qcli/docs/pages" },
  "api": { "title": "API reference", "openapi": "/qcli/api/openapi.json", "swagger": "/qcli/api/swagger" }
}
```

`docs` / `api` are `null` when that plugin's section was not added.

## Options

| Builder method | Default | Rules (`build()` raises `ValueError`) |
|---|---|---|
| `set_path(path)` | `/qcli/portal` | starts with `/`, no trailing `/` (except `/` itself), no `//`, `\`, whitespace, `?`, `#`, quotes, `<` or `>` (`invalid portal path: <path>`) |
| `set_name(name)` | `Documentation` | 1-80 characters without control characters (`portal name must be 1-80 characters without control characters`) |
| `set_logo_url(url)` | none | an http(s) URL, a root-relative path or a base64 `data:image/…` URL up to 200 000 characters (`portal logo must be an http(s) URL, a root-relative path or a data:image URL`) |
| `set_theme(theme)` | `{"mode": "system"}` | `mode` `dark`/`light`/`system` (`invalid portal theme mode: <mode>`); `dark` / `light` map the admin dashboard's 21 colour tokens (camelCase or snake_case) to colours the admin theme accepts (`invalid portal colour <scheme>.<token>: <value>`) |
| `add_footer_link(label, url)` | — | at most 20; label 1-80 characters, url http(s) or root-relative (`invalid portal footer link: <label>`) |
| `add_section(plugin)` | — | the docs or public-api plugin (or its `portal_section`); one per kind (`portal section already registered: <kind>`) |
| `set_site_dir(path)` | found | serve the site from this directory only |

## The site

Without `set_site_dir`, the site is looked up in this order: the npm package
(`node_modules/@qodalis/cli-server-portal/dist`, searched upwards from the working directory), the sibling repository's
build in a development checkout (`cli-server-dashboard/portal/dist`), then the copy embedded in the package
(`qodalis_cli_portal/site`). When none is found the plugin logs `[qcli] portal site not found; the portal is not served`
once and serves nothing, `portal.json` included.

Publishing the package embeds the site: `npm install` (pins `@qodalis/cli-server-portal` in `package.json`), then
`python scripts/copy_portal.py`, then build. Building a wheel or sdist without the site fails.

## License

MIT
