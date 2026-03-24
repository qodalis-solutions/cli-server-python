from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_LEVEL_ORDER = {
    "verbose": 0,
    "debug": 1,
    "information": 2,
    "warning": 3,
    "error": 4,
    "fatal": 5,
}


class CliLogSocketManager:
    """Manages WebSocket connections for streaming log messages to clients."""

    def __init__(self) -> None:
        self._clients: dict[int, tuple[WebSocket, str | None]] = {}
        self._next_id = 0

    async def handle_connection(
        self, websocket: WebSocket, level_filter: str | None = None
    ) -> None:
        """Accept a WebSocket connection for log streaming.

        Args:
            websocket: The incoming WebSocket connection.
            level_filter: Optional minimum log level to send to this client.
        """
        await websocket.accept()
        client_id = self._next_id
        self._next_id += 1
        self._clients[client_id] = (websocket, level_filter)
        logger.info("Log client connected (id=%s, level=%s)", client_id, level_filter or "all")

        try:
            await websocket.send_text(json.dumps({"type": "connected"}))

            while True:
                try:
                    await websocket.receive_text()
                except Exception:
                    break
        finally:
            self._clients.pop(client_id, None)
            logger.info("Log client disconnected (id=%s)", client_id)

    @staticmethod
    def should_send_log(filter_level: str | None, log_level: str) -> bool:
        """Check whether a log at *log_level* passes the *filter_level* gate."""
        if not filter_level:
            return True

        filter_ord = _LEVEL_ORDER.get(filter_level.lower())
        log_ord = _LEVEL_ORDER.get(log_level.lower())

        # Unknown levels always pass
        if filter_ord is None or log_ord is None:
            return True

        return log_ord >= filter_ord

    @staticmethod
    def format_log_message(level: str, message: str, category: str | None = None) -> str:
        """Return a JSON string representing a log envelope."""
        payload = {
            "type": "log",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.lower(),
            "message": message,
            "category": category,
        }
        return json.dumps(payload)

    def broadcast_log(self, level: str, message: str, category: str | None = None) -> None:
        """Send a log message to all connected clients whose filter allows it.

        The actual send is scheduled as a fire-and-forget async task.
        """
        formatted = self.format_log_message(level, message, category)

        targets: list[WebSocket] = []
        for ws, filter_level in self._clients.values():
            if self.should_send_log(filter_level, level):
                targets.append(ws)

        if not targets:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        loop.create_task(self._do_broadcast(targets, formatted))

    async def _do_broadcast(self, targets: list[WebSocket], message: str) -> None:
        tasks = [self._safe_send(ws, message) for ws in targets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast_disconnect(self) -> None:
        """Send a disconnect message to all clients and close connections."""
        logger.info("Broadcasting disconnect to %d log clients", len(self._clients))
        message = json.dumps({"type": "disconnect"})
        tasks = []
        for ws, _ in list(self._clients.values()):
            tasks.append(self._send_and_close(ws, message))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()

    @staticmethod
    async def _safe_send(client: WebSocket, message: str) -> None:
        try:
            await client.send_text(message)
        except Exception:
            logger.debug("Log broadcast send failed, removing client")

    @staticmethod
    async def _send_and_close(client: WebSocket, message: str) -> None:
        try:
            await client.send_text(message)
            await client.close()
        except Exception:
            logger.debug("Failed to send disconnect to log client")


class WebSocketLogHandler(logging.Handler):
    """A :class:`logging.Handler` that forwards records to a :class:`CliLogSocketManager`."""

    def __init__(self, manager: CliLogSocketManager) -> None:
        super().__init__()
        self.manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        """Format a log record and broadcast it via the socket manager."""
        level = self.map_log_level(record.levelno)
        category = record.name
        message = self.format(record) if self.formatter else record.getMessage()
        self.manager.broadcast_log(level, message, category)

    @staticmethod
    def map_log_level(levelno: int) -> str:
        """Map a Python logging level number to a string level name.

        Args:
            levelno: The numeric logging level.

        Returns:
            A string level name (debug, information, warning, error, or fatal).
        """
        if levelno <= logging.DEBUG:
            return "debug"
        elif levelno <= logging.INFO:
            return "information"
        elif levelno <= logging.WARNING:
            return "warning"
        elif levelno <= logging.ERROR:
            return "error"
        else:
            return "fatal"
