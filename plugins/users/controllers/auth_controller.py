"""Auth controller — /auth routes (login, logout, verify)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..models import LoginRequest, Session, VerifyRequest
from ..services.auth_service import AuthService
from ..models import SessionMetadata


def create_auth_router(
    auth_service: AuthService,
    get_current_session: Callable,
    *,
    external_auth: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    if external_auth:
        _managed_msg = {"error": "Authentication is managed by the host application"}

        @router.post("/login")
        async def login_external(body: LoginRequest):
            return JSONResponse(status_code=501, content=_managed_msg)

        @router.post("/logout")
        async def logout_external():
            return JSONResponse(status_code=501, content=_managed_msg)

        @router.post("/verify")
        async def verify_external(body: VerifyRequest):
            return JSONResponse(status_code=501, content=_managed_msg)

    else:
        @router.post("/login")
        async def login(body: LoginRequest, request: Request):
            metadata = SessionMetadata(
                remote_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            try:
                result = await auth_service.login(body.username, body.password, metadata)
                return {
                    "token": result["token"],
                    "session": result["session"].to_dict(),
                }
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc))

        @router.post("/logout", status_code=204)
        async def logout(request: Request, session: Session = Depends(get_current_session)):
            auth = request.headers.get("authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            await auth_service.logout(token)
            return Response(status_code=204)

        @router.post("/verify")
        async def verify(body: VerifyRequest):
            valid = await auth_service.verify_password(body.userId, body.password)
            return {"valid": valid}

    return router
