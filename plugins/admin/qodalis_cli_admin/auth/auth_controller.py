"""Authentication controller — login and ``/me`` endpoints."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .jwt_service import JwtService
from ..services.admin_config import AdminConfig


class LoginRequest(BaseModel):
    """Request body for the login endpoint."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Response body returned on successful login."""

    token: str
    expiresIn: int
    username: str


_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 60


class _RateLimiter:
    """Simple in-memory per-IP rate limiter for failed login attempts."""

    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str) -> None:
        """Raise ``HTTPException(429)`` if the IP has exceeded the limit."""
        now = time.time()
        cutoff = now - _WINDOW_SECONDS
        self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
        if len(self._attempts[ip]) >= _MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")

    def record_failure(self, ip: str) -> None:
        """Record a failed login attempt for the given IP."""
        self._attempts[ip].append(time.time())


ADMIN_ACCESS = "admin:access"


def trusted_hops(trust_proxy: bool | int | None) -> int:
    """Number of trusted reverse-proxy hops for a ``trust_proxy`` setting.

    ``False``/``0``/``None`` trust no proxy, ``True`` one hop, an int ``n``
    ``n`` hops; any other enabled value below 1 (a negative int) counts as
    one hop.  ``bool`` is checked first (``True == 1`` in Python).
    """
    if isinstance(trust_proxy, bool):
        return 1 if trust_proxy else 0
    if not trust_proxy:
        return 0
    return max(int(trust_proxy), 1)


def forwarded_client_ip(headers: Any, trust_proxy: bool | int | None) -> str | None:
    """Client IP from ``X-Forwarded-For`` behind ``trust_proxy`` trusted hops.

    Every proxy appends the address it saw, so only the rightmost ``n``
    entries were written by trusted proxies; the ones to their left are
    client-controlled.  The client IP is the entry at ``len - n`` of the
    comma-separated, trimmed list (the rightmost entry for one hop), or the
    leftmost entry when the list has fewer than ``n`` entries.  Returns
    ``None`` (use the socket peer) when no proxy is trusted, the header is
    absent/empty, or the selected entry is empty.
    """
    hops = trusted_hops(trust_proxy)
    if hops == 0:
        return None
    entries = [
        entry.strip()
        for value in headers.getlist("x-forwarded-for")
        for entry in value.split(",")
    ]
    if not entries:
        return None
    return entries[max(len(entries) - hops, 0)] or None


def client_ip(request: Request, trust_proxy: bool | int | None) -> str:
    """Client IP; ``X-Forwarded-For`` is only used with ``trust_proxy`` hops."""
    forwarded = forwarded_client_ip(request.headers, trust_proxy)
    if forwarded:
        return forwarded
    return request.client.host if request.client else "127.0.0.1"


def create_auth_router(
    admin_config: AdminConfig,
    jwt_service: JwtService,
    auth_dependency: Any = None,
    *,
    trust_proxy: bool | int = False,
    users_plugin: Any = None,
) -> APIRouter:
    """Create a FastAPI router with ``POST /login`` and ``GET /me``.

    With ``users_plugin`` the login goes through the users auth service and
    requires the ``admin:access`` permission; the returned token is the
    users session token.  Otherwise the static admin credentials are used.
    """
    router = APIRouter()
    limiter = _RateLimiter()

    @router.post("/login", response_model=LoginResponse)
    async def login(body: LoginRequest, request: Request) -> Any:
        ip = client_ip(request, trust_proxy)
        limiter.check(ip)

        if users_plugin is not None:
            return await _users_login(body, request, ip)

        if not admin_config.validate_credentials(body.username, body.password):
            limiter.record_failure(ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        expires_in = JwtService.DEFAULT_EXPIRY_SECONDS
        token = jwt_service.sign_token(
            {"username": body.username, "authenticatedAt": int(time.time())},
            expires_in=expires_in,
        )

        return LoginResponse(token=token, expiresIn=expires_in, username=body.username)

    async def _users_login(body: LoginRequest, request: Request, ip: str) -> Any:
        from qodalis_cli_users import SessionMetadata

        auth_service = users_plugin.auth_service
        await users_plugin.ensure_seeded()
        try:
            result = await auth_service.login(
                body.username, body.password,
                SessionMetadata(remote_address=ip, user_agent=request.headers.get("user-agent")),
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                limiter.record_failure(ip)
            raise
        allowed = any(_matches(p, ADMIN_ACCESS) for p in result.permissions)
        if not allowed or result.user.must_change_password:
            await auth_service.logout(result.token)
            raise HTTPException(
                status_code=403,
                detail="password change required" if allowed else f"missing permission: {ADMIN_ACCESS}",
            )
        return LoginResponse(
            token=result.token,
            expiresIn=auth_service.session_timeout_ms // 1000,
            username=result.user.name,
        )

    if auth_dependency is not None:
        @router.get("/me")
        async def me(payload: dict[str, Any] = Depends(auth_dependency)) -> dict[str, Any]:
            return {
                "username": payload.get("username", ""),
                "authenticatedAt": payload.get("authenticatedAt"),
            }

    return router


def _matches(granted: str, required: str) -> bool:
    g = granted.strip().lower().split(":")
    r = required.strip().lower().split(":")
    for i, seg in enumerate(g):
        # A trailing '*' grants everything below; elsewhere it matches
        # exactly one segment.
        if seg == "*" and i == len(g) - 1:
            return True
        if i >= len(r) or (seg != "*" and seg != r[i]):
            return False
    return len(g) == len(r)
