"""The databases API — ``/api/v1/qcli/databases``.

``GET /`` databases, ``GET /{db}`` one, ``GET /{db}/stats``, ``GET /{db}/migrations``,
``GET /{db}/migrations/runs``, ``GET /{db}/migrations/sql`` (dry run), ``POST /{db}/migrations/up``,
``POST /{db}/migrations/down``.  Errors answer ``{"error": "..."}``: 404 unknown database or migration, 400 bad
input, 409 busy or blocked.  On a protected server reading needs ``databases:read`` and running migrations
``databases:migrate`` (declared as route permissions).
"""

from __future__ import annotations

import functools
import json
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .databases import CliDatabases
from .runner import CliMigrationError, CliMigrationRunOptions

#: Key under ``scope["state"]`` where an auth plugin stores the caller (shared with ``qodalis_cli.security``).
AUTH_STATE_KEY = "qcli_auth"

TO_REQUIRED = "to is required to roll back (an id, or base for everything)"


def caller_name(ctx: Any) -> str | None:
    """The signed-in user's name from an auth context, if any."""
    if ctx is None:
        return None
    user = getattr(ctx, "user", None)
    name = getattr(user, "name", None) if user is not None else getattr(ctx, "user_name", None)
    return name if isinstance(name, str) and name else None


def _scope_caller(request: Request) -> Any:
    state = request.scope.get("state")
    if isinstance(state, dict):
        return state.get(AUTH_STATE_KEY)
    return getattr(state, AUTH_STATE_KEY, None) if state is not None else None


def _text(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 200:
        raise CliMigrationError(f"{name} must be a migration id", 400)
    return value


def _guarded(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Answers a refused request (:class:`CliMigrationError`) with its status and ``{"error"}``."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except CliMigrationError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)

    return wrapper


def create_databases_router(databases: CliDatabases) -> APIRouter:
    router = APIRouter()

    @router.get("")
    @router.get("/")
    @_guarded
    async def overview() -> Any:
        return {"databases": await databases.overview()}

    @router.get("/{db}")
    @_guarded
    async def database(db: str) -> Any:
        return await databases.summary(db)

    @router.get("/{db}/stats")
    @_guarded
    async def stats(db: str) -> Any:
        return await databases.stats(db)

    @router.get("/{db}/migrations")
    @_guarded
    async def database_migrations(db: str) -> Any:
        runner = databases.require(db)
        states = await runner.status()
        return {
            "name": runner.name,
            "dialect": runner.dialect,
            "sources": runner.source_names,
            "migrations": [s.to_dict() for s in states],
        }

    @router.get("/{db}/migrations/runs")
    @_guarded
    async def runs(db: str, request: Request) -> Any:
        def number(name: str, default: int) -> int:
            try:
                return int(request.query_params.get(name) or default)
            except ValueError:
                return default

        size = min(100, max(1, number("size", 20)))
        page = max(1, number("page", 1))
        items, total = await databases.require(db).runs(size, (page - 1) * size)
        return {"items": [r.to_dict() for r in items], "total": total}

    @router.get("/{db}/migrations/sql")
    @_guarded
    async def sql(db: str, request: Request) -> Any:
        runner = databases.require(db)
        direction = "down" if request.query_params.get("direction") == "down" else "up"
        to = _text(request.query_params.get("to"), "to")
        if direction == "down" and not to:
            raise CliMigrationError(TO_REQUIRED, 400)
        options = CliMigrationRunOptions(to=to, dry_run=True)
        return await (runner.up(options) if direction == "up" else runner.down(options))

    async def run(db: str, request: Request, direction: str) -> Any:
        try:
            body = await request.json() if await request.body() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CliMigrationError("invalid JSON body", 400) from None
        if not isinstance(body, dict):
            body = {}
        options = CliMigrationRunOptions(
            to=_text(body.get("to"), "to"),
            force=body.get("force") is True,
            actor=caller_name(_scope_caller(request)),
            trigger="api",
        )
        if direction == "down" and not options.to:
            raise CliMigrationError(TO_REQUIRED, 400)
        result = await databases.run(db, direction, options)  # type: ignore[arg-type]
        return result.to_dict()

    @router.post("/{db}/migrations/up")
    @_guarded
    async def up(db: str, request: Request) -> Any:
        return await run(db, request, "up")

    @router.post("/{db}/migrations/down")
    @_guarded
    async def down(db: str, request: Request) -> Any:
        return await run(db, request, "down")

    return router
