from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class CliEventSocketManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def handle_connection(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

        try:
            await websocket.send_text(json.dumps({"type": "connected"}))

            # Keep the connection open until the client disconnects
            while True:
                try:
                    await websocket.receive_text()
                except Exception:
                    break
        finally:
            self._clients.discard(websocket)

    async def broadcast_message(self, message: str) -> None:
        """Send a text message to all connected WebSocket clients."""
        for client in list(self._clients):
            try:
                await client.send_text(message)
            except Exception:
                pass

    async def broadcast_disconnect(self) -> None:
        message = json.dumps({"type": "disconnect"})
        tasks = []
        for client in list(self._clients):
            tasks.append(self._send_and_close(client, message))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()

    async def _send_and_close(self, client: WebSocket, message: str) -> None:
        try:
            await client.send_text(message)
            await client.close()
        except Exception:
            pass
