"""FastAPI dependencies for bearer-token authentication."""

from __future__ import annotations

import uuid
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request

from ..models import ExternalAuthResult, Session, User
from ..stores.interfaces import ISessionStore

# ---------------------------------------------------------------------------
# We use a factory approach: ``create_auth_dependencies`` returns callables
# that can be used with ``Depends``.  The session store is captured via
# closure rather than via ``request.app.state`` to keep the middleware
# self-contained and testable.
# ---------------------------------------------------------------------------


def create_auth_dependencies(
    session_store: ISessionStore,
    auth_adapter: Optional[Callable[[Request], Optional[ExternalAuthResult]]] = None,
):
    """Return a pair of FastAPI dependency callables:

    * ``get_current_session`` — extracts and validates the Bearer token,
      or delegates to an external auth adapter when provided.
    * ``require_admin`` — additionally checks for ``admin`` group membership.
    """

    if auth_adapter is not None:
        # ----- External auth adapter path -----
        async def get_current_session(request: Request) -> Session:
            result = auth_adapter(request)
            if result is None:
                raise HTTPException(status_code=401, detail="Unauthorized")
            groups = list(result.user.groups or [])
            if result.is_admin and "admin" not in groups:
                groups.append("admin")
            user = result.user
            user.groups = groups
            # Build a lightweight session — not persisted to the session store
            return Session(
                id=str(uuid.uuid4()),
                token="external",
                user_id=user.id,
                user=user,
            )
    else:
        # ----- Default bearer-token path -----
        async def get_current_session(request: Request) -> Session:
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Unauthorized")
            token = auth[7:]
            session = await session_store.get_session(token)
            if not session:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            await session_store.update_last_activity(token)
            return session

    async def require_admin(
        session: Session = Depends(get_current_session),
    ) -> Session:
        if "admin" not in (session.user.groups or []):
            raise HTTPException(status_code=403, detail="Forbidden: admin access required")
        return session

    return get_current_session, require_admin
