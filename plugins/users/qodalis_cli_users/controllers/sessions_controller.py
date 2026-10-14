"""Sessions controller — /sessions routes.

- ``POST   /sessions``          login: create a session from credentials (no auth)
- ``GET    /sessions/current``  current session
- ``DELETE /sessions/current``  logout: end the current session
- ``GET    /sessions``          list active sessions (admin)
- ``DELETE /sessions/{id}``     kick a session (admin)
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..models import LoginRequest, Session, SessionMetadata
from ..services.auth_service import AuthService
from ..stores.interfaces import ISessionStore

_MANAGED_BY_HOST = {"error": "Authentication is managed by the host application"}


def create_sessions_router(
    session_store: ISessionStore,
    auth_service: AuthService,
    get_current_session: Callable,
    require_admin: Callable,
    broadcast_event: Callable[[dict[str, Any]], None] | None = None,
    *,
    external_auth: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/sessions", tags=["sessions"])

    # POST / — login (no auth required)
    if external_auth:
        @router.post("")
        async def login_external(body: LoginRequest):
            return JSONResponse(status_code=501, content=_MANAGED_BY_HOST)

        # Declared before DELETE /{session_id} so "current" is not treated as an id
        @router.delete("/current")
        async def logout_external():
            return JSONResponse(status_code=501, content=_MANAGED_BY_HOST)
    else:
        @router.post("", status_code=201)
        async def login(body: LoginRequest, request: Request):
            metadata = SessionMetadata(
                remote_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            try:
                result = await auth_service.login(body.username, body.password, metadata)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc))
            return {
                "token": result["token"],
                "session": result["session"].to_dict(),
            }

        # Declared before DELETE /{session_id} so "current" is not treated as an id
        @router.delete("/current", status_code=204)
        async def logout(request: Request, session: Session = Depends(get_current_session)):
            auth = request.headers.get("authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            await auth_service.logout(token)
            return Response(status_code=204)

    # GET / — list all active sessions (admin only)
    @router.get("")
    async def list_sessions(session: Session = Depends(require_admin)):
        sessions = await session_store.get_active_sessions()
        return [s.to_dict() for s in sessions]

    # GET /current — return current session
    @router.get("/current")
    async def current_session(session: Session = Depends(get_current_session)):
        return session.to_dict()

    # DELETE /{session_id} — kick/force-logout a session (admin only)
    @router.delete("/{session_id}", status_code=204)
    async def kick_session(session_id: str, session: Session = Depends(require_admin)):
        target = await session_store.get_session_by_id(session_id)
        if not target:
            raise HTTPException(status_code=404, detail="Session not found")

        await session_store.delete_session_by_id(session_id)

        if broadcast_event:
            broadcast_event({
                "type": "session-kicked",
                "data": {"sessionId": target.id, "userId": target.user_id},
            })

        return Response(status_code=204)

    return router
