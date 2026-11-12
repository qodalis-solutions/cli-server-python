"""Link requests controller — ``/link`` (``server add`` in the web CLI,
through the browser).

- ``POST /link``                         a terminal asks to be linked (public, rate limited)
  → ``201 {linkId, userCode, verificationUrl, expiresIn, interval}``
- ``GET  /link/{linkId}``                the terminal polls (public) → ``{status}``, the token
  once approved; 404 ``{"status": "expired"}``
- ``GET  /link/code/{userCode}``         the approval page reads the request (signed in)
- ``POST /link/code/{userCode}/approve`` creates the approver's API token (session only)
- ``POST /link/code/{userCode}/deny``    → 204
"""

from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ..errors import UsersApiError, bad_request
from ..services.auth_service import AuthContext, AuthService
from ..services.link_service import LinkRateLimiter, LinkService
from ._routing import collection, new_router

TOO_MANY_LINK_REQUESTS = "too many link requests, try again later"


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise bad_request("invalid request body") from exc
    if not isinstance(body, dict):
        raise bad_request("invalid request body")
    return body


def create_link_router(
    auth_service: AuthService,
    links: LinkService,
    get_auth: Callable,
    limiter: LinkRateLimiter,
) -> APIRouter:
    router = new_router("/link", "link")

    @collection(router, "POST", status_code=201)
    async def create_link(request: Request):
        ip = auth_service.client_ip(request)
        if not limiter.try_acquire(ip):
            raise UsersApiError(429, TOO_MANY_LINK_REQUESTS)
        body = await _json_body(request)
        origin = request.headers.get("origin") or None
        return links.create(body, origin, ip)

    # /code/... before /{linkId}
    @router.get("/code/{code}")
    async def view_link(code: str, ctx: AuthContext = Depends(get_auth)):
        return links.view(code)

    @router.post("/code/{code}/approve")
    async def approve_link(code: str, ctx: AuthContext = Depends(get_auth)):
        return await links.approve(ctx, code)

    @router.post("/code/{code}/deny", status_code=204)
    async def deny_link(code: str, ctx: AuthContext = Depends(get_auth)):
        await links.deny(ctx, code)
        return Response(status_code=204)

    @router.get("/{link_id}")
    async def poll_link(link_id: str):
        result = links.poll(link_id)
        if result is None:
            return JSONResponse(status_code=404, content={"status": "expired"})
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    return router
