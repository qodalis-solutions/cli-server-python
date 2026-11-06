from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import WebSocket

from qodalis_cli_server_abstractions.events import CliEvent, CliEventAudience, ICliEventBus

from .session_bound_sockets import SessionBoundSockets

logger = logging.getLogger(__name__)

#: Events waiting for one socket; beyond this a slow client loses new events.
MAX_PENDING_EVENTS = 1000


class CliEventSocketManager(SessionBoundSockets):
    """Manages WebSocket connections for broadcasting server events to clients.

    Subscribed to the server's event bus (:meth:`attach`), it sends each event
    to the sockets of its audience: a socket's principal (the users plugin's
    auth context) must hold the event's permission or be its user.  Sockets
    without a principal receive every event while the server does not
    require authentication (read on every event, see
    :meth:`set_authentication_required`), else only events for everyone.

    Publishing never waits on a client: each socket has its own queue and
    sender task, so its events keep their order and a slow socket delays
    nobody else.

    Authenticated sockets are bound to their session: once it ends (logout,
    revoke, user disabled/deleted, expiry) the socket is closed with 4401
    ``session ended`` (see :class:`SessionBoundSockets`)."""

    def __init__(self) -> None:
        super().__init__()
        self._clients: set[WebSocket] = set()
        self._client_info: dict[WebSocket, dict[str, Any]] = {}
        self._principals: dict[WebSocket, Any] = {}
        self._outboxes: dict[WebSocket, asyncio.Queue[str]] = {}
        self._senders: dict[WebSocket, asyncio.Task[None]] = {}
        self._authentication_required: Callable[[], bool] | None = None
        self._next_client_id = 1

    def set_authentication_required(self, check: Callable[[], bool] | None) -> None:
        """``check()`` tells whether the server requires authentication (an
        auth plugin installs it when mounted); called on every event."""
        self._authentication_required = check

    def _requires_authentication(self) -> bool:
        check = self._authentication_required
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            logger.debug("Authentication check failed", exc_info=True)
            return True

    def attach(self, bus: ICliEventBus) -> Callable[[], None]:
        """Deliver every event of ``bus`` to the connected sockets of its
        audience; returns the unsubscribe function."""
        return bus.subscribe("*", self.publish_event)

    async def publish_event(self, event: CliEvent) -> None:
        """Queue ``event`` (flat JSON) for the sockets of its audience; the
        sends happen in each socket's sender task."""
        message = json.dumps(event.to_message(), default=str)
        requires_auth = self._requires_authentication()
        for client in list(self._clients):
            if self._receives(self._principals.get(client), event.audience, requires_auth):
                self._enqueue(client, message)

    def _enqueue(self, websocket: WebSocket, message: str) -> None:
        outbox = self._outboxes.get(websocket)
        if outbox is None:
            return
        try:
            outbox.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("Event socket too slow, event dropped")

    async def _send_loop(self, websocket: WebSocket, outbox: asyncio.Queue[str]) -> None:
        while True:
            message = await outbox.get()
            try:
                await websocket.send_text(message)
            except Exception:
                logger.debug("Event send failed")
                return

    def _stop_sender(self, websocket: WebSocket) -> None:
        self._outboxes.pop(websocket, None)
        sender = self._senders.pop(websocket, None)
        if sender is not None:
            sender.cancel()

    @staticmethod
    def _receives(principal: Any, audience: CliEventAudience, requires_auth: bool = False) -> bool:
        if audience.is_everyone:
            return True
        if principal is None:
            return not requires_auth
        if audience.user_id is not None:
            user = getattr(principal, "user", None)
            if user is not None and getattr(user, "id", None) == audience.user_id:
                return True
        if audience.permission is not None:
            can = getattr(principal, "can", None)
            try:
                if callable(can) and can(audience.permission):
                    return True
            except Exception:
                logger.debug("Permission check failed", exc_info=True)
        return False

    async def handle_connection(self, websocket: WebSocket, identity: Any = None) -> None:
        """Accept a WebSocket connection and keep it alive until the client disconnects.

        ``identity`` is the authenticated caller (an auth context exposing
        ``user.id`` and optionally ``session.id``) when the server is
        protected; user-scoped events are only delivered to such sockets.
        """
        if websocket.client_state.name == "CONNECTING":
            await websocket.accept()
        self._clients.add(websocket)
        if identity is not None:
            self._principals[websocket] = identity

        client_id = f"evt-{self._next_client_id}"
        self._next_client_id += 1

        remote_address = "unknown"
        if websocket.client:
            remote_address = websocket.client.host or "unknown"

        self._client_info[websocket] = {
            "id": client_id,
            "connectedAt": datetime.now(timezone.utc).isoformat(),
            "remoteAddress": remote_address,
            "type": "events",
        }
        user = getattr(identity, "user", None)
        session = getattr(identity, "session", None)
        if user is not None:
            self._client_info[websocket]["userId"] = getattr(user, "id", None)
            self._client_info[websocket]["userName"] = getattr(user, "name", None)
        if session is not None:
            self._client_info[websocket]["sessionId"] = getattr(session, "id", None)
        self.bind(websocket, identity)

        logger.info("Event client connected (id=%s)", client_id)

        outbox: asyncio.Queue[str] = asyncio.Queue(MAX_PENDING_EVENTS)
        outbox.put_nowait(json.dumps({"type": "connected"}))
        self._outboxes[websocket] = outbox
        self._senders[websocket] = asyncio.ensure_future(self._send_loop(websocket, outbox))

        try:
            while True:
                try:
                    await websocket.receive_text()
                except Exception:
                    break
        finally:
            self.unbind(websocket)
            self._stop_sender(websocket)
            self._clients.discard(websocket)
            self._client_info.pop(websocket, None)
            self._principals.pop(websocket, None)
            logger.info("Event client disconnected (id=%s)", client_id)

    def _on_session_ended(self, websocket: WebSocket) -> None:
        self._stop_sender(websocket)
        self._clients.discard(websocket)
        self._client_info.pop(websocket, None)
        self._principals.pop(websocket, None)

    async def broadcast_message(self, message: str) -> None:
        """Send a text message to all connected WebSocket clients (no
        audience: prefer ``event_bus.publish``)."""
        for client in list(self._clients):
            try:
                await client.send_text(message)
            except Exception:
                logger.debug("Broadcast send failed, removing client")

    async def send_to_user(self, user_id: str, message: str) -> None:
        """Send a message only to sockets authenticated as ``user_id``."""
        for client, info in list(self._client_info.items()):
            if info.get("userId") == user_id:
                try:
                    await client.send_text(message)
                except Exception:
                    logger.debug("User-scoped send failed")

    def get_clients(self) -> list[dict[str, Any]]:
        """Return information about all currently connected event clients."""
        return [
            dict(info) for info in self._client_info.values()
        ]

    async def broadcast_disconnect(self) -> None:
        """Send a disconnect message to all clients and close their connections."""
        logger.info("Broadcasting disconnect to %d event clients", len(self._clients))
        message = json.dumps({"type": "disconnect"})
        tasks = []
        for client in list(self._clients):
            tasks.append(self._send_and_close(client, message))
        for client in list(self._senders):
            self._stop_sender(client)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()
        self._client_info.clear()
        self._principals.clear()

    async def _send_and_close(self, client: WebSocket, message: str) -> None:
        try:
            await client.send_text(message)
            await client.close()
        except Exception:
            logger.debug("Broadcast send failed, removing client")
