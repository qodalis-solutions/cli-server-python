"""JWT token generation and validation using PyJWT."""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import jwt


class JwtService:
    """Thin wrapper around PyJWT for signing and verifying tokens."""

    DEFAULT_EXPIRY_SECONDS = 24 * 60 * 60  # 24 hours

    def __init__(self, secret: str = "") -> None:
        self._secret = (
            secret
            or os.environ.get("QCLI_ADMIN_JWT_SECRET", "")
            or secrets.token_hex(32)
        )

    @property
    def secret(self) -> str:
        return self._secret

    def sign_token(
        self,
        payload: dict[str, Any],
        expires_in: int | None = None,
    ) -> str:
        """Create a signed JWT token.

        Args:
            payload: Claims to embed in the token.
            expires_in: Lifetime in seconds. Defaults to 24 hours.
        """
        exp = int(time.time()) + (self.DEFAULT_EXPIRY_SECONDS if expires_in is None else expires_in)
        data = {**payload, "exp": exp, "iat": int(time.time())}
        return jwt.encode(data, self._secret, algorithm="HS256")

    def verify_token(self, token: str) -> dict[str, Any]:
        """Decode and verify a JWT token.

        Raises:
            jwt.InvalidTokenError: If the token is invalid or expired.
        """
        return jwt.decode(token, self._secret, algorithms=["HS256"])
