"""Error handling for the users routes.

Every error produced by the users routes — including FastAPI's request
validation errors (422 by default) and ``HTTPException``s raised by
dependencies — is returned as ``{"error": "<message>"}``; validation errors
become 400.  This is scoped to the users routers via a custom route class, so
other routers of the host app keep FastAPI's default behaviour.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

BODY_MAX_BYTES = 1024 * 1024


class UsersApiError(HTTPException):
    """An error with an HTTP status, rendered as ``{"error": message}`` on the
    users routes (and by the server-protection middleware).  Being an
    ``HTTPException`` it also works in host-app routes using the exported
    guards."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.message = message


def bad_request(message: str) -> UsersApiError:
    return UsersApiError(400, message)


def unauthorized(message: str = "unauthorized") -> UsersApiError:
    return UsersApiError(401, message)


def forbidden(message: str = "forbidden") -> UsersApiError:
    return UsersApiError(403, message)


def not_found(message: str) -> UsersApiError:
    return UsersApiError(404, message)


def conflict(message: str) -> UsersApiError:
    return UsersApiError(409, message)


def error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid request"
    first = errors[0]
    loc = [str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path")]
    field = ".".join(loc)
    err_type = first.get("type", "")
    if err_type == "missing":
        return f"{field} is required" if field else "request body is required"
    if err_type in ("json_invalid", "model_attributes_type") or not field:
        return "invalid request body"
    return f"invalid {field}: {first.get('msg', 'invalid value')}"


class UsersApiRoute(APIRoute):
    """Route class converting validation/HTTP errors to ``{error}`` bodies."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            length = request.headers.get("content-length")
            try:
                too_large = length is not None and int(length) > BODY_MAX_BYTES
            except ValueError:
                too_large = True
            if too_large or (
                request.method in ("POST", "PUT", "PATCH")
                and len(await request.body()) > BODY_MAX_BYTES
            ):
                return error_response(400, "request body too large")
            try:
                return await original(request)
            except UsersApiError as exc:
                return error_response(exc.status_code, exc.message)
            except RequestValidationError as exc:
                return error_response(400, _validation_message(exc))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"error": detail},
                    headers=getattr(exc, "headers", None),
                )

        return handler
