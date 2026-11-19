"""Adds the ``X-RateLimit-*`` headers of an admitted request to its answer.

The guard stores them in ``request.state.qcli_rate_headers``
(``scope["state"]``); this pure ASGI middleware copies them onto the response
start message, so the handler's own answers (errors included) carry them.
Headers the handler set itself are left as they are.
"""

from __future__ import annotations

from typing import Any

STATE_KEY = "qcli_rate_headers"


class RateLimitHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Created here so a scope copied further in still shares the dict.
        state = scope.setdefault("state", {})

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = state.get(STATE_KEY) if isinstance(state, dict) else None
                if headers:
                    existing = list(message.get("headers") or [])
                    present = {name.lower() for name, _ in existing}
                    for name, value in headers.items():
                        raw = name.lower().encode("latin-1")
                        if raw not in present:
                            existing.append((raw, str(value).encode("latin-1")))
                    message = {**message, "headers": existing}
            await send(message)

        await self.app(scope, receive, send_with_headers)
