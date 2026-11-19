# qodalis-cli-server-plugin-public-api

A public REST API for the host application of a [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python):
the host's own routes, guarded by `host` API tokens of the [users plugin](../users/README.md), counted and limited per
account, with usage per token on the token views (`GET /api/v1/qcli/tokens`, the admin dashboard).

## Install

```bash
pip install qodalis-cli-server-plugin-public-api
```

## Quick Start

```python
from fastapi import Depends
from qodalis_cli import CliPermission, CliServerOptions, create_cli_server
from qodalis_cli_public_api import CliPublicApiBuilder, PublicApiQuota
from qodalis_cli_users import ApiTokenCreateOptions, CliUsersBuilder, QcliCaller

result = create_cli_server(CliServerOptions(configure=lambda b: b.add_permissions([
    CliPermission(name="rates:read", description="Official exchange rates", group="Rates"),
])))
users = CliUsersBuilder().build()
public_api = (
    CliPublicApiBuilder()
    .set_users(users)
    .set_quota(lambda caller: PublicApiQuota(requests_per_day=1000, requests_per_minute=60))
    .build()
)
result.mount_plugin(users)
result.mount_plugin(public_api)   # after the users plugin


@result.app.get("/v1/rates")
async def rates(caller: QcliCaller = Depends(public_api.guard("rates:read"))):
    return {"account": caller.user_id, "rates": [...]}

# The host creates its customers' tokens (shown once):
# await users.api_tokens.create(user.id, ApiTokenCreateOptions(name="site", audience="host", permissions=["rates:read"]))
```

Mount the plugin with `mount_plugin`: it then answers refusals as `{"error": "..."}`, adds the rate-limit headers and
the token views' `usage`. Its routes are host routes (`cli_routes = False`), never treated as CLI routes.

## The guard

`public_api.guard(permission=None)` is a FastAPI dependency returning the `QcliCaller` (also
`request.state.qcli_caller`). It checks, in this order, and the first failure answers (nothing is counted):

| # | Condition | Answer |
|---|---|---|
| 1 | authentication fails | 401 `unauthorized` / 401 `invalid or expired token` / 403 `account disabled` / 403 `password change required` |
| 2 | not a `host` token (a session, an adapter user, a `cli` token) | 403 `forbidden: a host token is required` |
| 3 | `permission` given and not held | 403 `forbidden: missing permission <permission>` |
| 4 | the quota resolver raises | 500 `internal server error` (logged; the text never reaches the client) |
| 5 | a limit is `0` (or negative) | 403 `forbidden: no API access` |
| 6 | the account's requests today reached `requests_per_day` | 429 `quota exceeded: <n> requests per day`, `Retry-After` = seconds to the next UTC midnight |
| 7 | the account's requests this minute reached `requests_per_minute` | 429 `too many requests: <n> per minute`, `Retry-After` = seconds to the next UTC minute |
| — | otherwise | the request is counted, then the handler runs |

The guard carries `qcli_permission` and `qcli_host_token_only = True`: the public OpenAPI document shows them as
`x-required-permission` and `x-host-token-only`.

## Quotas

`set_quota(resolver)`: `resolver(caller)` (sync or async) returns a `PublicApiQuota(requests_per_day=None,
requests_per_minute=None)` for the caller's account (`None` = unlimited, `0` = no access). Without a resolver the API
is unlimited (requests are still counted for usage).

The quota is **per account**: requests are counted per token (for display) and the limits are checked against the sum
over every token of the user, revoked tokens included, for the current UTC day (`yyyy-MM-dd`) and UTC minute (fixed
windows). More tokens never mean more requests, and revoking a token does not reset the count.

### Headers

Every answer of a guarded route that passed the permission check (429s and the handler's own answers included; not
401/403/500) carries:

| Header | Value |
|---|---|
| `X-RateLimit-Limit` | `requests_per_day` when set, else `requests_per_minute` |
| `X-RateLimit-Remaining` | that limit minus the account's requests in the window, this one included (0 on a 429) |
| `X-RateLimit-Reset` | seconds until that window resets (at least 1) |
| `Retry-After` | on a 429: seconds until the exceeded window resets |

No headers are sent when both limits are `None`.

## Usage

- `GET /api/v1/qcli/tokens` and `users.api_tokens.list(user_id)` add `usage` to each token view:
  `{"today": 12, "last30Days": 340, "days": [{"date": "2026-10-21", "requests": 0}, ...]}` (30 days, oldest first,
  the last one today in UTC). Creating a token returns no `usage`.
- `await public_api.usage.account(user_id)` returns a `PublicApiAccountUsage` (`today`, `quota`, `tokens`:
  `PublicApiTokenUsage(token_id, name, usage)`; `to_dict()` gives the camelCase JSON) for the host's own pages; an
  unknown user raises `UsersApiError` 404 `user not found`.

## OpenAPI document and Swagger UI

| Route | Answer |
|---|---|
| `GET <base>/openapi.json` | 200 `application/json; charset=utf-8`, `Cache-Control: no-cache`, `Access-Control-Allow-Origin: *`: the public document. No sign-in; credentials are ignored |
| `GET <base>/swagger`, `<base>/swagger/` | 200 `text/html`: Swagger UI 5 for that document only (`persistAuthorization: false`, `tryItOutEnabled: true`), titled with the document's `info.title` |

`<base>` is `/qcli/api` (`set_base_path(path)`: starts with `/`, no trailing `/`, not `/` itself, no `//`, whitespace,
`?`, `#` or `\`; otherwise `build()` raises `ValueError("invalid public-api base path: <path>")`). Other paths under it
are not handled. Both routes answer HEAD too and are served without sign-in, also under `/api/` on a protected server
(the plugin's `public_paths`). Under an ASGI `root_path` the `servers` URL, the Swagger page's document URL and its
root-relative assets include it; a `root_path` starting with `//` or `/\` answers 400 `{"error": "invalid path base"}`.

Hand the document over with `set_openapi(source)`:

| Source | Document |
|---|---|
| `APIRouter` / `FastAPI` | `get_openapi(title=<set_title or "API">, version=<set_version or "1.0.0">, routes=source.routes)`, read on the first request; routes with `include_in_schema=False` are left out |
| `dict` | used as given; it must have `openapi`, `info` and `paths` (`ValueError("public-api: invalid OpenAPI document")` otherwise) |
| zero-argument callable | called per request, its `dict` used as given |

With `set_openapi(app)` **every route of that app** appears in the public document, so never hand over the server's
own app (it holds the CLI's routes). Keep the public routes on a dedicated router (or app, or a schema) and include it
in the server's app. A router is read with its own paths: one included with a prefix
(`app.include_router(public, prefix="/v1")`) must be built with that prefix (`APIRouter(prefix="/v1")`), or its paths
lose it in the document.

```python
public = APIRouter()
public_api = CliPublicApiBuilder().set_users(users).set_openapi(public).set_title("RateBell API").build()

@public.get("/v1/rates", tags=["Rates"])
async def rates(caller=Depends(public_api.guard("rates:read"))): ...

result.app.include_router(public)
result.mount_plugin(public_api)
```

The plugin adds, unless the document has its own (the host's values always win):

- `components.securitySchemes.qcliToken` (`http` bearer, `bearerFormat: qcli_pat`) and a top-level
  `security: [{"qcliToken": []}]`;
- `servers: [{"url": <set_server_url(url) or the request's scheme://host and root path>}]`;
- for routers and apps, `x-required-permission` and `x-host-token-only: true` on each operation guarded by
  `public_api.guard(...)` or `users.require_host_token(...)` (`x-required-permission` alone for
  `users.require_permission(...)`).

The host's own `x-codeSamples` are passed through untouched; the portal generates the other code samples.
Swagger UI is served from files vendored in this package: **swagger-ui-dist 5.35.1** (Apache-2.0; its `LICENSE`,
`NOTICE` and the bundle's licence notes are in `qodalis_cli_public_api/swagger_ui/`), at `<base>/swagger/swagger-ui-bundle.js`,
`<base>/swagger/swagger-ui.css` and `<base>/swagger/favicon-32x32.png`. Only these three files are served; anything else
under `<base>/swagger/` is 404 `{"error": "not found"}`. The page loads nothing from another origin, and every URL in
it is escaped. `set_swagger_assets(js_url, css_url)` points the page at copies the host serves itself (root-relative
paths only; anything else raises `ValueError("public-api: Swagger UI assets must be root-relative paths")`).

Stacked guards (a router's `dependencies=[Depends(public_api.guard())]` and a route's
`Depends(public_api.guard("rates:read"))`) count the request once; the later ones only check their own permission.
A failing usage store answers 500 `internal server error` and the counts already made for that request are taken
back (best effort).

## In production

- **Set the URLs explicitly.** Without `set_server_url(url)` the document's `servers` (and, in the docs plugin, without
  `set_base_url(url)`, `{{baseUrl}}`) come from the request's `Host` header. Behind a proxy or with an untrusted
  `Host`, set them.
- **"Try it" from another origin.** The portal's "try it" box and Swagger UI call the host's API from the browser.
  When the portal is served from another origin than the API, the API needs CORS (`CliServerOptions(cors_origins=[...])`
  or the host's own CORS middleware) with `Authorization` allowed, and `Access-Control-Expose-Headers:
  X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After` so the page can read the rate-limit
  headers.
- **Root path.** Under an ASGI `root_path` the document's URLs include it; a `root_path` starting with `//` or `/\`
  answers 400 `{"error": "invalid path base"}` (the docs plugin and the portal do the same).

## Usage store

The counters live in an `IPublicApiUsageStore` (`increment`, `decrement`, `account_count`, `token_days`, `prune`); the
default `InMemoryPublicApiUsageStore` keeps them in this process. Set a store shared by every process
(`set_usage_store(store)`) to apply the limits across processes: a request is admitted by incrementing the token's day
and minute counters, reading the account's sums and, when a limit is exceeded, decrementing them again, so the count
never goes over the limit. Day counters are kept `set_usage_retention_days(days)` days (default 31), minute counters
two minutes; the guard prunes at most once an hour.

## Options

| Builder method | Default | |
|---|---|---|
| `set_users(users_plugin)` | — | required (`build()` raises `ValueError("public-api: set_users(users_plugin) is required")`) |
| `set_quota(resolver)` | unlimited | the account's limits |
| `set_usage_store(store)` | in memory | the counters |
| `set_usage_retention_days(days)` | `31` | days of per-day counters kept |
| `set_base_path(path)` | `/qcli/api` | where the OpenAPI document and Swagger UI are served |
| `set_title(title)` | `API reference` | the portal's label of the API section (and `info.title` of a router or app document, default `API`) |
| `set_version(version)` | `1.0.0` | `info.version` of a router or app document |
| `set_openapi(source)` | an empty document | the public document |
| `set_server_url(url)` | the request's URL | the document's `servers` |
| `set_swagger_assets(js_url, css_url)` | the vendored files | Swagger UI assets (root-relative paths) |
| `set_clock(now)` | UTC now | tests |

The plugin's `portal_section` (`{"kind": "api", "title", "base_path"}`) is what the portal plugin shows.

## Tokens and several services

Several services may *validate* tokens over shared stores (a guard-only users plugin, `set_guard_only()`), but tokens
must be *created and revoked* from one service only: the token name's uniqueness and the 20-token limit are checked in
the process that creates the token.

## License

MIT
