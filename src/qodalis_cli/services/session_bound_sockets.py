"""Binds authenticated events/logs WebSockets to the session that opened
them: when that session ends (logout, revoke, user disabled/deleted, expiry)
the socket is closed with 4401 ``session ended``."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Union

from fastapi import WebSocket

logger = logging.getLogger(__name__)

CLOSE_SESSION_ENDED = 4401
REASON_SESSION_ENDED = "session ended"

#: ``check(identity) -> bool``: ``True`` while the identity's session is alive.
SessionCheck = Callable[[Any], Union[bool, Awaitable[bool]]]


class SessionBoundSockets:
    """Tracks the identity of authenticated sockets and closes those whose
    session ended.  An auth plugin installs the check with
    :meth:`set_session_check` and calls :meth:`request_recheck` whenever
    sessions, users or groups change."""

    def __init__(self) -> None:
        self._session_check: SessionCheck | None = None
        self._bound: dict[WebSocket, tuple[Any, asyncio.AbstractEventLoop]] = {}

    def set_session_check(self, check: SessionCheck | None) -> None:
        self._session_check = check

    def bind(self, websocket: WebSocket, identity: Any) -> None:
        if identity is not None and getattr(identity, "user", None) is not None:
            self._bound[websocket] = (identity, asyncio.get_running_loop())

    def unbind(self, websocket: WebSocket) -> None:
        self._bound.pop(websocket, None)

    def request_recheck(self) -> None:
        """Re-check every bound socket now (thread-safe, non-blocking)."""
        if self._session_check is None:
            return
        for websocket, (identity, loop) in list(self._bound.items()):
            try:
                loop.call_soon_threadsafe(self._spawn_check, websocket, identity)
            except RuntimeError:
                pass  # loop closed

    def _spawn_check(self, websocket: WebSocket, identity: Any) -> None:
        if websocket in self._bound:
            asyncio.ensure_future(self._check(websocket, identity))

    async def _check(self, websocket: WebSocket, identity: Any) -> None:
        check = self._session_check
        if check is None:
            return
        try:
            result = check(identity)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.exception("Socket session check failed; closing the socket")
            result = False
        if result or websocket not in self._bound:
            return
        self.unbind(websocket)
        self._on_session_ended(websocket)
        try:
            await websocket.close(code=CLOSE_SESSION_ENDED, reason=REASON_SESSION_ENDED)
        except Exception as e:
            logger.debug("Failed to close socket of an ended session: %s", e)

    def _on_session_ended(self, websocket: WebSocket) -> None:
        """Hook: stop delivering messages to ``websocket``."""
