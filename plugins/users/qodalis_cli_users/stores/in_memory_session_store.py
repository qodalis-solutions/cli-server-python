"""In-memory implementation of ISessionStore."""

from __future__ import annotations

import time
import uuid

from ..models import Session, SessionMetadata, User
from .interfaces import ISessionStore


class InMemorySessionStore(ISessionStore):
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}  # keyed by token

    async def create_session(
        self,
        user_id: str,
        token: str,
        user: User,
        metadata: SessionMetadata,
    ) -> Session:
        now = int(time.time() * 1000)
        session = Session(
            id=str(uuid.uuid4()),
            token=token,
            user_id=user_id,
            user=user,
            login_time=now,
            last_activity=now,
            metadata=metadata,
        )
        self._sessions[token] = session
        return session

    async def get_session(self, token: str) -> Session | None:
        return self._sessions.get(token)

    async def get_session_by_id(self, session_id: str) -> Session | None:
        for session in self._sessions.values():
            if session.id == session_id:
                return session
        return None

    async def get_active_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    async def delete_session(self, token: str) -> None:
        self._sessions.pop(token, None)

    async def delete_session_by_id(self, session_id: str) -> None:
        token_to_delete: str | None = None
        for token, session in self._sessions.items():
            if session.id == session_id:
                token_to_delete = token
                break
        if token_to_delete is not None:
            del self._sessions[token_to_delete]

    async def delete_user_sessions(self, user_id: str) -> None:
        tokens_to_delete = [
            token for token, session in self._sessions.items()
            if session.user_id == user_id
        ]
        for token in tokens_to_delete:
            del self._sessions[token]

    async def update_last_activity(self, token: str) -> None:
        session = self._sessions.get(token)
        if session:
            session.last_activity = int(time.time() * 1000)

    async def cleanup(self, max_age_ms: int) -> None:
        cutoff = int(time.time() * 1000) - max_age_ms
        tokens_to_delete = [
            token for token, session in self._sessions.items()
            if session.last_activity < cutoff
        ]
        for token in tokens_to_delete:
            del self._sessions[token]
