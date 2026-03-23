from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class CliEventSocketManager:
    """Manages WebSocket connections for broadcasting server events to clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._client_info: dict[WebSocket, dict[str, Any]] = {}
        self._next_client_id = 1

    async def handle_connection(self, websocket: WebSocket) -> None:
        """Accept a WebSocket connection and keep it alive until the client disconnects."""
        await websocket.accept()
        self._clients.add(websocket)

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

        try:
            await websocket.send_text(json.dumps({"type": "connected"}))

            while True:
                try:
                    await websocket.receive_text()
                except Exception:
                    break
        finally:
            self._clients.discard(websocket)
            self._client_info.pop(websocket, None)

    async def broadcast_message(self, message: str) -> None:
        """Send a text message to all connected WebSocket clients."""
        for client in list(self._clients):
            try:
                await client.send_text(message)
            except Exception:
                pass

    def get_clients(self) -> list[dict[str, Any]]:
        """Return information about all currently connected event clients."""
        return [
            dict(info) for info in self._client_info.values()
        ]

    async def broadcast_disconnect(self) -> None:
        """Send a disconnect message to all clients and close their connections."""
        message = json.dumps({"type": "disconnect"})
        tasks = []
        for client in list(self._clients):
            tasks.append(self._send_and_close(client, message))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()
        self._client_info.clear()

    async def _send_and_close(self, client: WebSocket, message: str) -> None:
        try:
            await client.send_text(message)
            await client.close()
        except Exception:
            pass
