"""Authentication controller — login and ``/me`` endpoints."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .jwt_service import JwtService
from ..services.admin_config import AdminConfig


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expiresIn: int
    username: str


# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------

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
        # Clean old entries
        self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
        if len(self._attempts[ip]) >= _MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")

    def record_failure(self, ip: str) -> None:
        self._attempts[ip].append(time.time())


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_auth_router(
    admin_config: AdminConfig,
    jwt_service: JwtService,
    auth_dependency: Any = None,
) -> APIRouter:
    """Create a FastAPI router with ``POST /login`` and ``GET /me``."""
    router = APIRouter()
    limiter = _RateLimiter()

    @router.post("/login", response_model=LoginResponse)
    async def login(body: LoginRequest, request: Request) -> Any:
        # Check X-Forwarded-For header for real client IP behind proxy
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else "127.0.0.1"
        limiter.check(client_ip)

        if not admin_config.validate_credentials(body.username, body.password):
            limiter.record_failure(client_ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        expires_in = JwtService.DEFAULT_EXPIRY_SECONDS
        token = jwt_service.sign_token(
            {"username": body.username, "authenticatedAt": int(time.time())},
            expires_in=expires_in,
        )

        return LoginResponse(token=token, expiresIn=expires_in, username=body.username)

    if auth_dependency is not None:
        @router.get("/me")
        async def me(payload: dict[str, Any] = Depends(auth_dependency)) -> dict[str, Any]:
            return {
                "username": payload.get("username", ""),
                "authenticatedAt": payload.get("authenticatedAt"),
            }

    return router
