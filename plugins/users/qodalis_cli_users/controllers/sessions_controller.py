"""Sessions controller — ``/sessions``.

- ``POST   /sessions``          login (public)
- ``GET    /sessions/current``  current session (authenticated)
- ``DELETE /sessions/current``  logout (authenticated)
- ``GET    /sessions``          list sessions (``sessions:read``)
- ``DELETE /sessions/{id}``     revoke (``sessions:revoke`` or own session)
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Request, Response

from ..errors import error_response, forbidden, not_found
from ..models import LoginRequest, SessionMetadata
from ..permissions import SESSIONS_READ, SESSIONS_REVOKE
from ..services.auth_service import AuthContext, AuthService
from ._routing import collection, new_router

MANAGED_BY_HOST = "Authentication is managed by the host application"


def create_sessions_router(
    auth_service: AuthService,
    get_auth: Callable,
    require_permission: Callable[[str], Callable],
    ensure_ready: Callable | None = None,
) -> APIRouter:
    router = new_router("/sessions", "sessions")
    external = auth_service.external_auth

    if external:
        @collection(router, "POST")
        async def login_external():
            return error_response(501, MANAGED_BY_HOST)

        # Declared before DELETE /{session_id} so "current" is not taken as an id.
        @router.delete("/current")
        async def logout_external():
            return error_response(501, MANAGED_BY_HOST)
    else:
        @collection(router, "POST", status_code=201)
        async def login(body: LoginRequest, request: Request):
            if ensure_ready is not None:
                await ensure_ready()
            metadata = SessionMetadata(
                remote_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            result = await auth_service.login(body.username, body.password, metadata)
            return {
                "token": result.token,
                "session": auth_service.session_payload(
                    result.session, result.user, result.permissions,
                ),
            }

        @router.delete("/current", status_code=204)
        async def logout(ctx: AuthContext = Depends(get_auth)):
            if ctx.token:
                await auth_service.logout(ctx.token)
            return Response(status_code=204)

    @router.get("/current")
    async def current_session(ctx: AuthContext = Depends(get_auth)):
        assert ctx.session is not None
        return auth_service.session_payload(ctx.session, ctx.user, ctx.permissions)

    @collection(router, "GET")
    async def list_sessions(ctx: AuthContext = Depends(require_permission(SESSIONS_READ))):
        result = []
        for session in await auth_service.session_store.get_active_sessions():
            if auth_service.is_expired(session):
                await auth_service.session_store.delete_session(session.token)
                continue
            user = await auth_service.user_store.get_user(session.user_id)
            result.append(session.to_listing_dict(
                auth_service.session_timeout_ms,
                user_name=user.name if user else None,
                current=ctx.session is not None and session.id == ctx.session.id,
            ))
        return result

    @router.delete("/{session_id}", status_code=204)
    async def revoke_session(session_id: str, ctx: AuthContext = Depends(get_auth)):
        target = await auth_service.session_store.get_session_by_id(session_id)
        if not target:
            raise not_found(f"session not found: {session_id}")
        if target.user_id != ctx.user.id and not ctx.can(SESSIONS_REVOKE):
            raise forbidden(f"missing permission: {SESSIONS_REVOKE}")
        user = await auth_service.user_store.get_user(target.user_id)
        await auth_service.revoke_session(
            target, "revoked",
            actor=ctx.user.name,
            username=user.name if user else None,
        )
        return Response(status_code=204)

    return router
