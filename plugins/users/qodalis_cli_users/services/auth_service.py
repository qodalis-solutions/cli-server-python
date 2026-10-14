"""Authentication service — credentials, login/logout, lockout, sessions,
effective permissions, session revocation and auditing."""

from __future__ import annotations

import inspect
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional, Union

from ..errors import UsersApiError, unauthorized
from ..models import (
    AuditEntry,
    ExternalAuthResult,
    Session,
    SessionMetadata,
    User,
    now_ms,
)
from ..permissions import ADMIN_GROUP, has_permission, merge_permissions
from ..stores.interfaces import (
    IAuditStore,
    ICredentialStore,
    IGroupStore,
    ISessionStore,
    IUserStore,
)
from . import password_hasher

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TIMEOUT_MS = 30 * 60 * 1000
DEFAULT_MIN_PASSWORD_LENGTH = 4
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
DEFAULT_LOCKOUT_MS = 5 * 60 * 1000

ACCOUNT_LOCKED = "account locked, try again later"
ACCOUNT_DISABLED = "account disabled"
INVALID_CREDENTIALS = "invalid credentials"

#: Receives the JSON-encoded event; may be sync or async.
BroadcastFn = Callable[[str], Union[Awaitable[None], None]]


@dataclass
class AuthContext:
    """The authenticated caller of a request."""

    user: User
    groups: list[str]
    permissions: list[str]
    session: Session | None = None
    token: str | None = None
    external: bool = False

    def can(self, permission: str) -> bool:
        return has_permission(self.permissions, permission)

    def is_self(self, user: User | None) -> bool:
        return user is not None and user.id == self.user.id


@dataclass
class LoginResult:
    token: str
    session: Session
    user: User
    permissions: list[str]


@dataclass
class _LockoutState:
    failures: int = 0
    locked_until: int = 0


@dataclass
class AuthServiceOptions:
    session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS
    min_password_length: int = DEFAULT_MIN_PASSWORD_LENGTH
    max_login_attempts: int = DEFAULT_MAX_LOGIN_ATTEMPTS
    lockout_ms: int = DEFAULT_LOCKOUT_MS


class AuthService:
    """Handles authentication, sessions and the security bookkeeping shared by
    the controllers (lockout, revocation broadcasts, audit)."""

    def __init__(
        self,
        user_store: IUserStore,
        session_store: ISessionStore,
        credential_store: ICredentialStore,
        audit_store: IAuditStore,
        group_store: IGroupStore | None = None,
        *,
        options: AuthServiceOptions | None = None,
        broadcast_fn: BroadcastFn | None = None,
        auth_adapter: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        if group_store is None:
            from ..stores.in_memory_group_store import InMemoryGroupStore
            group_store = InMemoryGroupStore()
        self.user_store = user_store
        self.session_store = session_store
        self.credential_store = credential_store
        self.audit_store = audit_store
        self.group_store = group_store
        self.options = options or AuthServiceOptions()
        self.broadcast_fn = broadcast_fn
        self.auth_adapter = auth_adapter
        self._lockouts: dict[str, _LockoutState] = {}

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    @property
    def session_timeout_ms(self) -> int:
        return self.options.session_timeout_ms

    @property
    def external_auth(self) -> bool:
        return self.auth_adapter is not None

    # ------------------------------------------------------------------
    # Passwords
    # ------------------------------------------------------------------

    @staticmethod
    def generate_token() -> str:
        return secrets.token_hex(32)

    def validate_password_policy(self, password: str | None) -> str | None:
        """Return an error message when ``password`` violates the policy."""
        if password is None or password == "":
            return "password is required"
        if len(password) < self.options.min_password_length:
            return f"password must be at least {self.options.min_password_length} characters"
        return None

    async def set_password(self, user_id: str, password: str) -> None:
        encoded, salt = password_hasher.hash_password(password)
        await self.credential_store.set_password(user_id, encoded, salt)

    async def verify_password(self, user_id: str, password: str) -> bool:
        creds = await self.credential_store.get_credentials(user_id)
        if not creds:
            return False
        valid = password_hasher.verify_password(password, creds.password_hash, creds.salt)
        if valid and password_hasher.needs_rehash(creds.password_hash):
            await self.set_password(user_id, password)
        return valid

    # ------------------------------------------------------------------
    # Lockout
    # ------------------------------------------------------------------

    def is_locked(self, user_id: str) -> bool:
        state = self._lockouts.get(user_id)
        if not state or not state.locked_until:
            return False
        if state.locked_until > now_ms():
            return True
        self._lockouts.pop(user_id, None)  # lock expired
        return False

    def register_failure(self, user_id: str) -> None:
        state = self._lockouts.setdefault(user_id, _LockoutState())
        state.failures += 1
        if state.failures >= self.options.max_login_attempts:
            state.failures = 0
            state.locked_until = now_ms() + self.options.lockout_ms

    def reset_failures(self, user_id: str) -> None:
        self._lockouts.pop(user_id, None)

    # ------------------------------------------------------------------
    # Users / groups / permissions
    # ------------------------------------------------------------------

    async def find_user(self, id_name_or_email: str) -> User | None:
        """Resolve a user by id, name or email."""
        user = await self.user_store.get_user(id_name_or_email)
        if user is None and "@" in id_name_or_email:
            user = await self.user_store.get_user_by_email(id_name_or_email)
        return user

    async def effective_permissions(self, groups: list[str]) -> list[str]:
        lists: list[list[str]] = []
        for name in groups:
            group = await self.group_store.get_group(name)
            if group:
                lists.append(group.permissions)
        return merge_permissions(*lists)

    @staticmethod
    def in_group(user: User, group: str) -> bool:
        g = group.lower()
        return any(x.lower() == g for x in user.groups or [])

    async def is_last_enabled_admin(self, user: User) -> bool:
        if user.disabled or not self.in_group(user, ADMIN_GROUP):
            return False
        admins = [
            u for u in await self.user_store.get_users()
            if not u.disabled and self.in_group(u, ADMIN_GROUP)
        ]
        return len(admins) <= 1

    # ------------------------------------------------------------------
    # Auditing / broadcasting
    # ------------------------------------------------------------------

    async def audit(
        self,
        event: str,
        username: str,
        *,
        details: str | None = None,
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.audit_store.log(AuditEntry(
                event=event,
                username=username,
                details=details,
                actor=actor,
                metadata=metadata,
            ))
        except Exception:
            logger.exception("Failed to write audit entry %s", event)

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self.broadcast_fn:
            return
        try:
            result = self.broadcast_fn(json.dumps(message))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Failed to broadcast %s", message.get("type"))

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def is_expired(self, session: Session) -> bool:
        return now_ms() - session.last_activity > self.session_timeout_ms

    async def revoke_session(
        self,
        session: Session,
        reason: str,
        *,
        actor: str | None = None,
        username: str | None = None,
    ) -> None:
        """Delete a session, broadcast ``session-kicked`` and audit it."""
        await self.session_store.delete_session_by_id(session.id)
        await self.broadcast({
            "type": "session-kicked",
            "data": {"sessionId": session.id, "userId": session.user_id, "reason": reason},
        })
        await self.audit(
            "session_revoked",
            username or session.user_name,
            details=reason,
            actor=actor,
            metadata={"sessionId": session.id},
        )

    async def revoke_user_sessions(
        self,
        user_id: str,
        reason: str,
        *,
        actor: str | None = None,
        except_session_id: str | None = None,
    ) -> int:
        count = 0
        for session in await self.session_store.get_active_sessions():
            if session.user_id == user_id and session.id != except_session_id:
                await self.revoke_session(session, reason, actor=actor)
                count += 1
        return count

    async def cleanup_sessions(self) -> None:
        await self.session_store.cleanup(self.session_timeout_ms)

    def session_payload(
        self, session: Session, user: User, permissions: list[str],
    ) -> dict[str, Any]:
        """The ``session`` object of the login / current-session responses."""
        return {
            "id": session.id,
            "user": user.to_dict(),
            "permissions": list(permissions),
            "loginTime": session.created_at,
            "lastActivity": session.last_activity,
            "expiresAt": session.expires_at(self.session_timeout_ms),
        }

    # ------------------------------------------------------------------
    # Request authentication
    # ------------------------------------------------------------------

    async def authenticate_token(self, token: str) -> AuthContext:
        """Resolve a bearer token to the live user; raises 401 otherwise."""
        session = await self.session_store.get_session(token)
        if not session:
            raise unauthorized("invalid or expired token")
        if self.is_expired(session):
            await self.session_store.delete_session(token)
            raise unauthorized("session expired")
        user = await self.user_store.get_user(session.user_id)
        if not user or user.id != session.user_id or user.disabled:
            raise unauthorized("invalid or expired token")
        await self.session_store.update_last_activity(token)
        session.last_activity = now_ms()
        groups = list(user.groups or [])
        return AuthContext(
            user=user,
            groups=groups,
            permissions=await self.effective_permissions(groups),
            session=session,
            token=token,
        )

    async def authenticate_external(self, request: Any) -> AuthContext:
        assert self.auth_adapter is not None
        result = self.auth_adapter(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ExternalAuthResult) or result.user is None:
            raise unauthorized()
        groups = list(result.user.groups or [])
        if result.is_admin and not any(g.lower() == ADMIN_GROUP for g in groups):
            groups.append(ADMIN_GROUP)
        # Copy — never mutate the adapter's user object.
        user = replace(result.user, groups=groups)
        now = now_ms()
        session = Session(
            id=f"external-{uuid.uuid4()}",
            token="",
            user_id=user.id,
            user_name=user.name,
            created_at=now,
            last_activity=now,
        )
        return AuthContext(
            user=user,
            groups=groups,
            permissions=await self.effective_permissions(groups),
            session=session,
            external=True,
        )

    # ------------------------------------------------------------------
    # Login / logout
    # ------------------------------------------------------------------

    async def login(
        self,
        username: str,
        password: str,
        metadata: SessionMetadata | None = None,
    ) -> LoginResult:
        """Authenticate by name or email and create a session.

        Raises :class:`UsersApiError` (401 invalid credentials, 403 disabled,
        429 locked).
        """
        metadata = metadata or SessionMetadata()
        audit_meta = {"remoteAddress": metadata.remote_address}

        user = await self.find_user(username)
        if not user:
            await self.audit("login_failed", username, details="unknown user", metadata=audit_meta)
            raise UsersApiError(401, INVALID_CREDENTIALS)

        if self.is_locked(user.id):
            await self.audit("login_failed", user.name, details="account locked", metadata=audit_meta)
            raise UsersApiError(429, ACCOUNT_LOCKED)

        if not await self.verify_password(user.id, password):
            self.register_failure(user.id)
            await self.audit("login_failed", user.name, details="wrong password", metadata=audit_meta)
            raise UsersApiError(401, INVALID_CREDENTIALS)

        self.reset_failures(user.id)

        if user.disabled:
            await self.audit("login_failed", user.name, details="account disabled", metadata=audit_meta)
            raise UsersApiError(403, ACCOUNT_DISABLED)

        user = await self.user_store.update_user(user.id, last_login_at=now_ms())
        token = self.generate_token()
        session = await self.session_store.create_session(
            user.id, token, metadata, user_name=user.name,
        )
        await self.audit(
            "login", user.name,
            metadata={**audit_meta, "sessionId": session.id},
        )
        groups = list(user.groups or [])
        return LoginResult(
            token=token,
            session=session,
            user=user,
            permissions=await self.effective_permissions(groups),
        )

    async def logout(self, token: str) -> None:
        session = await self.session_store.get_session(token)
        await self.session_store.delete_session(token)
        if session:
            user = await self.user_store.get_user(session.user_id)
            name = user.name if user else session.user_name
            await self.audit("logout", name, metadata={"sessionId": session.id})

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    async def seed(self, default_admin_password: str) -> None:
        """Seed the built-in groups and (unless external auth) the root user."""
        from ..permissions import USERS_GROUP

        if not await self.group_store.get_group(ADMIN_GROUP):
            await self.group_store.create_group(
                ADMIN_GROUP, "Administrators", ["*"], built_in=True,
            )
        if not await self.group_store.get_group(USERS_GROUP):
            await self.group_store.create_group(
                USERS_GROUP, "Default group for new accounts", [], built_in=True,
            )
        if self.external_auth:
            return
        if len(await self.user_store.get_users()) == 0:
            root = await self.user_store.create_user(
                name="root", email="root@localhost", groups=[ADMIN_GROUP],
            )
            await self.set_password(root.id, default_admin_password)
