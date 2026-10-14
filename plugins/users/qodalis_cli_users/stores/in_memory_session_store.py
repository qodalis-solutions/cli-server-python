"""In-memory implementation of ISessionStore."""

from __future__ import annotations

import copy

from ..models import Session, SessionMetadata, now_ms
from .interfaces import ISessionStore


class InMemorySessionStore(ISessionStore):
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}  # keyed by token hash

    async def create_session(
        self,
        user_id: str,
        token_hash: str,
        metadata: SessionMetadata,
        *,
        user_name: str = "",
    ) -> Session:
        now = now_ms()
        session = Session(
            token_hash=token_hash,
            user_id=user_id,
            user_name=user_name,
            created_at=now,
            last_activity=now,
            metadata=metadata or SessionMetadata(),
        )
        self._sessions[token_hash] = session
        return copy.deepcopy(session)

    async def get_session(self, token_hash: str) -> Session | None:
        session = self._sessions.get(token_hash)
        return copy.deepcopy(session) if session else None

    async def get_session_by_id(self, session_id: str) -> Session | None:
        for session in self._sessions.values():
            if session.id == session_id:
                return copy.deepcopy(session)
        return None

    async def get_active_sessions(self) -> list[Session]:
        return [copy.deepcopy(s) for s in self._sessions.values()]

    async def delete_session(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)

    async def delete_session_by_id(self, session_id: str) -> None:
        for token, session in list(self._sessions.items()):
            if session.id == session_id:
                del self._sessions[token]
                return

    async def delete_user_sessions(self, user_id: str) -> None:
        for token, session in list(self._sessions.items()):
            if session.user_id == user_id:
                del self._sessions[token]

    async def update_last_activity(self, token_hash: str) -> None:
        session = self._sessions.get(token_hash)
        if session:
            session.last_activity = now_ms()

    async def cleanup(self, max_age_ms: int) -> None:
        cutoff = now_ms() - max_age_ms
        for token, session in list(self._sessions.items()):
            if session.last_activity < cutoff:
                del self._sessions[token]
