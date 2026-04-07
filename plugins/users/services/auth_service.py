"""Authentication service — password hashing, login/logout, token generation."""

from __future__ import annotations

import hashlib
import secrets
import time

from ..models import AuditEntry, Session, SessionMetadata, User
from ..stores.interfaces import (
    IAuditStore,
    ICredentialStore,
    ISessionStore,
    IUserStore,
)


class AuthService:
    """Handles authentication logic (password hashing, login, logout)."""

    def __init__(
        self,
        user_store: IUserStore,
        session_store: ISessionStore,
        credential_store: ICredentialStore,
        audit_store: IAuditStore,
    ) -> None:
        self._user_store = user_store
        self._session_store = session_store
        self._credential_store = credential_store
        self._audit_store = audit_store

    # ------------------------------------------------------------------
    # Password helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.sha256((salt + password).encode()).hexdigest()

    @staticmethod
    def generate_token() -> str:
        return secrets.token_hex(32)

    async def set_password(self, user_id: str, password: str) -> None:
        salt = secrets.token_hex(16)
        hash_ = self._hash_password(password, salt)
        await self._credential_store.set_password(user_id, hash_, salt)

    async def verify_password(self, user_id: str, password: str) -> bool:
        creds = await self._credential_store.get_credentials(user_id)
        if not creds:
            return False
        return self._hash_password(password, creds.salt) == creds.password_hash

    # ------------------------------------------------------------------
    # Login / Logout
    # ------------------------------------------------------------------

    async def login(
        self,
        username: str,
        password: str,
        metadata: SessionMetadata | None = None,
    ) -> dict:
        """Authenticate a user and create a session.

        Returns ``{"token": ..., "session": ...}`` on success.
        Raises ``ValueError`` on failure.
        """
        if metadata is None:
            metadata = SessionMetadata()

        now = int(time.time() * 1000)

        user = await self._user_store.get_user(username)
        if not user:
            await self._audit_store.log(AuditEntry(
                timestamp=now,
                event="login_failed",
                username=username,
                details="User not found",
                metadata={"remoteAddress": metadata.remote_address},
            ))
            raise ValueError("Invalid credentials")

        if user.disabled:
            await self._audit_store.log(AuditEntry(
                timestamp=now,
                event="login_failed",
                username=username,
                details="Account disabled",
                metadata={"remoteAddress": metadata.remote_address},
            ))
            raise ValueError("Account is disabled")

        valid = await self.verify_password(user.id, password)
        if not valid:
            await self._audit_store.log(AuditEntry(
                timestamp=now,
                event="login_failed",
                username=username,
                details="Wrong password",
                metadata={"remoteAddress": metadata.remote_address},
            ))
            raise ValueError("Invalid credentials")

        # Update lastLoginAt
        user.last_login_at = now

        token = self.generate_token()
        session = await self._session_store.create_session(
            user.id, token, user, metadata,
        )

        await self._audit_store.log(AuditEntry(
            timestamp=now,
            event="login",
            username=username,
            details="Login successful",
            metadata={
                "remoteAddress": metadata.remote_address,
                "sessionId": session.id,
            },
        ))

        return {"token": token, "session": session}

    async def logout(self, token: str) -> None:
        session = await self._session_store.get_session(token)
        username = session.user.name if session else "unknown"

        await self._session_store.delete_session(token)

        await self._audit_store.log(AuditEntry(
            timestamp=int(time.time() * 1000),
            event="logout",
            username=username,
            details="User logged out",
            metadata={"sessionId": session.id if session else None},
        ))
