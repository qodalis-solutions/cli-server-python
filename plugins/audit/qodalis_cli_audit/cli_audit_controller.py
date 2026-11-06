"""The audit log API — ``/api/v1/qcli/audit``.

``GET`` lists entries (``audit:read`` on a protected server), ``DELETE``
clears the log (``audit:clear``).  The admin dashboard's Audit page and the
CLI's ``audit`` command read it.  Refusals answer ``{"error": "..."}``, like
the users API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from qodalis_cli_server_abstractions.audit import AuditEntry, AuditQueryOptions

if TYPE_CHECKING:
    from .cli_audit_builder import CliAuditPlugin

#: Entries returned when the request names no limit.
DEFAULT_AUDIT_LIMIT = 100
#: The most entries one request returns, so a database store never loads the
#: whole log.
MAX_AUDIT_LIMIT = 1000
#: The event recorded after the log was cleared.
AUDIT_CLEARED_EVENT = "audit_cleared"

AUDIT_READ = "audit:read"
AUDIT_CLEAR = "audit:clear"

#: Key under ``scope["state"]`` where an auth plugin stores the caller
#: (shared with ``qodalis_cli.security``; kept local so the plugin does not
#: depend on the server package).
AUTH_STATE_KEY = "qcli_auth"

BAD_PAGING = "limit and offset must be integers >= 0"


def _error(status: int, message: Any) -> JSONResponse:
    return JSONResponse({"error": str(message)}, status_code=status)


def _scope_caller(request: Request) -> Any:
    state = request.scope.get("state")
    if isinstance(state, dict):
        return state.get(AUTH_STATE_KEY)
    return getattr(state, AUTH_STATE_KEY, None) if state is not None else None


def _paging(request: Request, name: str) -> int | None:
    """``None`` when absent or empty; raises ``ValueError`` when not an integer >= 0."""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    value = int(raw.strip())  # ValueError for "x", "1.5", ...
    if value < 0:
        raise ValueError(name)
    return value


def _time(request: Request, name: str) -> int | None:
    """``from``/``to`` as Unix time in ms: ISO 8601 (no offset = UTC) or
    epoch milliseconds; ``None`` when absent; ``ValueError`` when neither."""
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{name}' must be an ISO 8601 time or epoch milliseconds, got '{raw}'") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def create_audit_router(plugin: CliAuditPlugin) -> APIRouter:
    """The audit routes.  They read ``plugin.store`` on every request, so a
    store set after the router was built is used."""
    router = APIRouter(tags=["audit"])

    async def caller_of(request: Request, permission: str) -> Any:
        if plugin.authorize is not None:
            return await plugin.authorize(request, permission)
        caller = _scope_caller(request)
        # Defensive: the route policy of an auth plugin already checked it.
        can = getattr(caller, "can", None)
        if callable(can) and not can(permission):
            raise HTTPException(403, f"missing permission: {permission}")
        return caller

    async def list_entries(request: Request) -> Any:
        """Entries newest first, filtered by ``event``, ``username`` (the user
        an entry is about) and ``actor`` (who did it), all exact and
        case-insensitive, and the time range ``from`` (inclusive) / ``to``
        (exclusive; ISO 8601 or epoch milliseconds); paged with ``limit`` (default 100, at most 1000) and
        ``offset``."""
        try:
            await caller_of(request, AUDIT_READ)
        except HTTPException as exc:
            return _error(exc.status_code, exc.detail)
        try:
            limit = _paging(request, "limit")
            offset = _paging(request, "offset")
        except ValueError:
            return _error(400, BAD_PAGING)
        try:
            start = _time(request, "from")
            end = _time(request, "to")
        except ValueError as exc:
            return _error(400, exc)
        query = request.query_params
        entries = await plugin.store.get_entries(AuditQueryOptions(
            event=query.get("event") or None,
            username=query.get("username") or None,
            actor=query.get("actor") or None,
            limit=DEFAULT_AUDIT_LIMIT if limit is None else min(limit, MAX_AUDIT_LIMIT),
            offset=offset or None,
            from_=start,
            to=end,
        ))
        return [entry.to_dict() for entry in entries]

    async def clear_entries(request: Request) -> Response:
        """Clears the log, then records who cleared it (``audit_cleared``)."""
        try:
            caller = await caller_of(request, AUDIT_CLEAR)
        except HTTPException as exc:
            return _error(exc.status_code, exc.detail)
        await plugin.store.clear()
        user = getattr(caller, "user", None)
        name = getattr(user, "name", None)
        user_id = getattr(user, "id", None)
        await plugin.log(AuditEntry(
            event=AUDIT_CLEARED_EVENT,
            username=name or "",
            user_id=user_id,
            details="audit log cleared",
            actor=name,
            actor_id=user_id,
            metadata={"remoteAddress": request.client.host if request.client else None},
        ))
        return Response(status_code=204)

    # Both "" and "/" so neither form redirects.
    for path, in_schema in (("", True), ("/", False)):
        router.add_api_route(path, list_entries, methods=["GET"], include_in_schema=in_schema)
        router.add_api_route(
            path, clear_entries, methods=["DELETE"], status_code=204,
            response_class=Response, include_in_schema=in_schema,
        )
    return router
