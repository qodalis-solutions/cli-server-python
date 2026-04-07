"""Sessions controller — /sessions routes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Response

from ..models import Session
from ..stores.interfaces import ISessionStore


def create_sessions_router(
    session_store: ISessionStore,
    get_current_session: Callable,
    require_admin: Callable,
    broadcast_event: Callable[[dict[str, Any]], None] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/sessions", tags=["sessions"])

    # GET / — list all active sessions (admin only)
    @router.get("/")
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
