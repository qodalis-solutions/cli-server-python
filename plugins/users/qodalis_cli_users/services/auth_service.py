"""Authentication service — credentials, login/logout, lockout, rate limits,
sessions, WebSocket tickets, effective permissions, revocation and audit."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import secrets
import string

import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional, Union

from ..errors import UsersApiError, forbidden, unauthorized
from ..models import (
    AuditEntry,
    ExternalAuthResult,
    Session,
    SessionMetadata,
    User,
    now_ms,
)
from ..permissions import ADMIN_GROUP, USERS_GROUP, has_permission, merge_permissions, permission_matches
from ..stores.interfaces import (
    IAuditStore,
    ICredentialStore,
    IGroupStore,
    ISessionStore,
    IUserStore,
)
from ..validation import PASSWORD_MAX, sanitize
from . import password_hasher

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TIMEOUT_MS = 30 * 60 * 1000
DEFAULT_MAX_SESSION_LIFETIME_MS = 12 * 60 * 60 * 1000
DEFAULT_MIN_PASSWORD_LENGTH = 8
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
DEFAULT_LOCKOUT_MS = 5 * 60 * 1000
DEFAULT_RATE_LIMIT_ATTEMPTS = 20
DEFAULT_RATE_LIMIT_WINDOW_MS = 60 * 1000
TICKET_TTL_MS = 30 * 1000
DEFAULT_USERS_GROUP_PERMISSIONS = ["commands:execute"]
WEAK_ADMIN_PASSWORDS = {"root", "admin", "password"}

ACCOUNT_LOCKED = "account locked, try again later"
ACCOUNT_DISABLED = "account disabled"
INVALID_CREDENTIALS = "invalid credentials"
PASSWORD_CHANGE_REQUIRED = "password change required"
TOO_MANY_REQUESTS = "too many requests, try again later"
INSUFFICIENT_PRIVILEGES = "insufficient privileges for this user"

_MAX_TRACKED_KEYS = 10_000

#: Receives the JSON-encoded event; may be sync or async.
BroadcastFn = Callable[[str], Union[Awaitable[None], None]]
#: ``(user_id, json_message)`` — delivers to sockets of that user only.
UserEventSender = Callable[[str, str], Awaitable[None]]

#: Audit events that never change who may do what (no listener notification).
_NON_CHANGING_EVENTS = frozenset({"login", "login_failed", "shell_opened", "shell_closed"})


def hash_token(token: str) -> str:
    """SHA-256 (hex) of a bearer token or ticket."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def covers(granted: list[str], required: list[str]) -> bool:
    """True when every permission in ``required`` is matched by ``granted``."""
    return all(has_permission(granted, p) for p in required)


def uncovered(granted: list[str], required: list[str]) -> list[str]:
    return [p for p in required if not has_permission(granted, p)]


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

    @property
    def must_change_password(self) -> bool:
        return bool(self.user.must_change_password)


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
class _Ticket:
    session_id: str | None
    user_id: str
    expires_at: int
    external_ctx: AuthContext | None = None


@dataclass
class AuthServiceOptions:
    session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS
    max_session_lifetime_ms: int = DEFAULT_MAX_SESSION_LIFETIME_MS
    min_password_length: int = DEFAULT_MIN_PASSWORD_LENGTH
    max_login_attempts: int = DEFAULT_MAX_LOGIN_ATTEMPTS
    lockout_ms: int = DEFAULT_LOCKOUT_MS
    rate_limit_attempts: int = DEFAULT_RATE_LIMIT_ATTEMPTS
    rate_limit_window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS
    trust_proxy: bool = False
    #: Origins accepted for state-changing requests without a bearer token
    #: (CSRF protection in adapter/cookie mode).  Same-origin requests are
    #: always accepted.
    allowed_origins: list[str] | None = None
    ticket_ttl_ms: int = TICKET_TTL_MS


class AuthService:
    """Handles authentication, sessions and the security bookkeeping shared by
    the controllers and the server-protection middleware."""

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
        #: Set when mounted on a CLI server: user-scoped event delivery.
        self.user_event_sender: UserEventSender | None = None
        self.auth_adapter = auth_adapter
        self._lockouts: OrderedDict[str, _LockoutState] = OrderedDict()
        self._rate: OrderedDict[str, deque[int]] = OrderedDict()
        self._tickets: dict[str, _Ticket] = {}
        self._change_listeners: list[Callable[[], Any]] = []

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    @property
    def session_timeout_ms(self) -> int:
        return self.options.session_timeout_ms

    @property
    def max_session_lifetime_ms(self) -> int:
        return self.options.max_session_lifetime_ms

    @property
    def external_auth(self) -> bool:
        return self.auth_adapter is not None

    # ------------------------------------------------------------------
    # Passwords
    # ------------------------------------------------------------------

    @staticmethod
    def generate_token() -> str:
        return secrets.token_hex(32)

    @staticmethod
    def generate_password(length: int = 16) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def validate_password_policy(self, password: str | None) -> str | None:
        """Return an error message when ``password`` violates the policy."""
        if password is None or password == "":
            return "password is required"
        if len(password) < self.options.min_password_length:
            return f"password must be at least {self.options.min_password_length} characters"
        if len(password) > PASSWORD_MAX:
            return f"password must be at most {PASSWORD_MAX} characters"
        return None

    async def set_password(self, user_id: str, password: str) -> None:
        encoded, salt = await password_hasher.hash_password_async(password)
        await self.credential_store.set_password(user_id, encoded, salt)

    async def verify_password(self, user_id: str, password: str) -> bool:
        creds = await self.credential_store.get_credentials(user_id)
        if not creds:
            await password_hasher.dummy_verify_async(password)
            return False
        if len(password) > PASSWORD_MAX:
            return False
        valid = await password_hasher.verify_password_async(password, creds.password_hash, creds.salt)
        if valid and password_hasher.needs_rehash(creds.password_hash):
            await self.set_password(user_id, password)
        return valid

    # ------------------------------------------------------------------
    # Lockout (atomic: the attempt is reserved before PBKDF2 runs)
    # ------------------------------------------------------------------

    @staticmethod
    def lockout_key(user: User | None, attempted: str) -> str:
        if user is not None:
            return user.id
        return "unknown:" + attempted.strip().lower()[:256]

    def _state(self, key: str, create: bool) -> _LockoutState | None:
        state = self._lockouts.get(key)
        if state is None and create:
            state = _LockoutState()
            self._lockouts[key] = state
            while len(self._lockouts) > _MAX_TRACKED_KEYS:
                self._lockouts.popitem(last=False)
        return state

    def is_locked(self, key: str) -> bool:
        state = self._lockouts.get(key)
        if not state or not state.locked_until:
            return False
        if state.locked_until > now_ms():
            return True
        self._lockouts.pop(key, None)  # lock expired
        return False

    def reserve_attempt(self, key: str) -> bool:
        """Count an attempt *before* verifying it.  Returns ``False`` when the
        key is locked.  Synchronous, so concurrent requests cannot interleave
        between the check and the increment."""
        if self.is_locked(key):
            return False
        state = self._state(key, True)
        assert state is not None
        state.failures += 1
        if state.failures >= self.options.max_login_attempts:
            state.failures = 0
            state.locked_until = now_ms() + self.options.lockout_ms
        return True

    def reset_failures(self, key: str) -> None:
        self._lockouts.pop(key, None)

    # Backwards-compatible alias
    def register_failure(self, key: str) -> None:
        self.reserve_attempt(key)

    # ------------------------------------------------------------------
    # Per-IP rate limit
    # ------------------------------------------------------------------

    def client_ip(self, request: Any) -> str:
        if self.options.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                first = forwarded.split(",")[0].strip()
                if first:
                    return first
        client = getattr(request, "client", None)
        return (client.host if client else None) or "unknown"

    def check_rate_limit(self, request: Any) -> None:
        """Count one attempt for the caller's IP; 429 above the limit."""
        ip = self.client_ip(request)
        now = now_ms()
        window = self._rate.get(ip)
        if window is None:
            window = deque()
            self._rate[ip] = window
            while len(self._rate) > _MAX_TRACKED_KEYS:
                self._rate.popitem(last=False)
        cutoff = now - self.options.rate_limit_window_ms
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.options.rate_limit_attempts:
            raise UsersApiError(429, TOO_MANY_REQUESTS)
        window.append(now)

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

    async def user_permissions(self, user: User) -> list[str]:
        return await self.effective_permissions(list(user.groups or []))

    async def ensure_covers_user(self, ctx: AuthContext, target: User) -> None:
        """B3: a caller may only act on users whose permissions it holds."""
        if ctx.is_self(target):
            return
        if not covers(ctx.permissions, await self.user_permissions(target)):
            raise forbidden(INSUFFICIENT_PRIVILEGES)

    async def ensure_can_grant_groups(self, ctx: AuthContext, groups: list[str]) -> None:
        """B3: adding a user to ``groups`` requires holding their permissions."""
        missing = uncovered(ctx.permissions, await self.effective_permissions(groups))
        if missing:
            raise forbidden(f"cannot grant permissions you do not hold: {', '.join(missing)}")

    @staticmethod
    def ensure_can_grant_permissions(ctx: AuthContext, permissions: list[str]) -> None:
        missing = uncovered(ctx.permissions, permissions)
        if missing:
            raise forbidden(f"cannot grant permissions you do not hold: {', '.join(missing)}")

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
    # Auditing / events
    # ------------------------------------------------------------------

    async def audit(
        self,
        event: str,
        username: str,
        *,
        details: str | None = None,
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        actor_id: str | None = None,
    ) -> None:
        try:
            await self.audit_store.log(AuditEntry(
                event=event,
                username=sanitize(username, 256) or "",
                details=sanitize(details, 1024),
                actor=sanitize(actor, 256),
                metadata=metadata,
                user_id=user_id,
                actor_id=actor_id,
            ))
        except Exception:
            logger.exception("Failed to write audit entry %s", event)

        if event not in _NON_CHANGING_EVENTS:
            self.notify_change()

    # ------------------------------------------------------------------
    # Change listeners (e.g. open shells re-check their session)
    # ------------------------------------------------------------------

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        """Call ``listener()`` after sessions, users, groups or permissions
        change (logout, revoke, disable, delete, group/permission edits,
        expiry sweep).  Listeners must not block."""
        if listener not in self._change_listeners:
            self._change_listeners.append(listener)

    def remove_change_listener(self, listener: Callable[[], Any]) -> None:
        if listener in self._change_listeners:
            self._change_listeners.remove(listener)

    def notify_change(self) -> None:
        for listener in list(self._change_listeners):
            try:
                listener()
            except Exception:
                logger.exception("Change listener failed")

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self.broadcast_fn:
            return
        try:
            result = self.broadcast_fn(json.dumps(message))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Failed to broadcast %s", message.get("type"))

    async def notify_user(self, user_id: str, message: dict[str, Any]) -> None:
        """Deliver a user-scoped event only to that user's event sockets
        (falls back to ``broadcast_fn`` when not mounted on a CLI server)."""
        if self.user_event_sender is not None:
            try:
                await self.user_event_sender(user_id, json.dumps(message))
            except Exception:
                logger.exception("Failed to send %s", message.get("type"))
            return
        await self.broadcast(message)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def is_expired(self, session: Session) -> bool:
        return now_ms() > session.expires_at(self.session_timeout_ms, self.max_session_lifetime_ms)

    def expires_at(self, session: Session) -> int:
        return session.expires_at(self.session_timeout_ms, self.max_session_lifetime_ms)

    async def revoke_session(
        self,
        session: Session,
        reason: str,
        *,
        actor: str | None = None,
        actor_id: str | None = None,
        username: str | None = None,
    ) -> None:
        """Delete a session, notify the user's sockets and audit it."""
        await self.session_store.delete_session_by_id(session.id)
        self._drop_tickets(session.id)
        await self.notify_user(session.user_id, {
            "type": "session-kicked",
            "data": {"sessionId": session.id, "userId": session.user_id, "reason": reason},
        })
        await self.audit(
            "session_revoked",
            username or session.user_name,
            details=reason,
            actor=actor,
            actor_id=actor_id,
            user_id=session.user_id,
            metadata={"sessionId": session.id},
        )

    async def revoke_user_sessions(
        self,
        user_id: str,
        reason: str,
        *,
        actor: str | None = None,
        actor_id: str | None = None,
        except_session_id: str | None = None,
    ) -> int:
        count = 0
        for session in await self.session_store.get_active_sessions():
            if session.user_id == user_id and session.id != except_session_id:
                await self.revoke_session(session, reason, actor=actor, actor_id=actor_id)
                count += 1
        return count

    async def cleanup_sessions(self) -> None:
        await self.session_store.cleanup(self.session_timeout_ms)
        for session in await self.session_store.get_active_sessions():
            if self.is_expired(session):
                await self.session_store.delete_session_by_id(session.id)
        self.notify_change()
        now = now_ms()
        for key, ticket in list(self._tickets.items()):
            if ticket.expires_at <= now:
                self._tickets.pop(key, None)

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
            "expiresAt": self.expires_at(session),
        }

    # ------------------------------------------------------------------
    # Request authentication
    # ------------------------------------------------------------------

    async def _context_for_session(self, session: Session) -> AuthContext:
        if self.is_expired(session):
            await self.session_store.delete_session_by_id(session.id)
            raise unauthorized("session expired")
        user = await self.user_store.get_user(session.user_id)
        if not user or user.id != session.user_id or user.disabled:
            raise unauthorized("invalid or expired token")
        groups = list(user.groups or [])
        return AuthContext(
            user=user,
            groups=groups,
            permissions=await self.effective_permissions(groups),
            session=session,
        )

    async def authenticate_token(self, token: str) -> AuthContext:
        """Resolve a bearer token to the live user; raises 401 otherwise."""
        token_hash = hash_token(token)
        session = await self.session_store.get_session(token_hash)
        if not session:
            raise unauthorized("invalid or expired token")
        ctx = await self._context_for_session(session)
        await self.session_store.update_last_activity(token_hash)
        session.last_activity = now_ms()
        ctx.token = token
        return ctx

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

    @staticmethod
    def ensure_password_changed(ctx: AuthContext) -> None:
        if ctx.must_change_password:
            raise forbidden(PASSWORD_CHANGE_REQUIRED)

    # ------------------------------------------------------------------
    # WebSocket tickets
    # ------------------------------------------------------------------

    def issue_ticket(self, ctx: AuthContext) -> tuple[str, int]:
        """Single-use ticket bound to the caller's session; returns
        ``(ticket, expires_in_seconds)``."""
        now = now_ms()
        for key, t in list(self._tickets.items()):
            if t.expires_at <= now:
                self._tickets.pop(key, None)
        ticket = secrets.token_hex(32)
        self._tickets[hash_token(ticket)] = _Ticket(
            session_id=None if ctx.external else (ctx.session.id if ctx.session else None),
            user_id=ctx.user.id,
            expires_at=now + self.options.ticket_ttl_ms,
            external_ctx=ctx if ctx.external else None,
        )
        return ticket, self.options.ticket_ttl_ms // 1000

    def _drop_tickets(self, session_id: str) -> None:
        for key, t in list(self._tickets.items()):
            if t.session_id == session_id:
                self._tickets.pop(key, None)

    async def consume_ticket(self, ticket: str | None) -> AuthContext | None:
        """Validate and consume a ticket; ``None`` when invalid/expired/used."""
        if not ticket:
            return None
        entry = self._tickets.pop(hash_token(ticket), None)
        if entry is None or entry.expires_at <= now_ms():
            return None
        if entry.external_ctx is not None:
            return entry.external_ctx
        if entry.session_id is None:
            return None
        session = await self.session_store.get_session_by_id(entry.session_id)
        if session is None or session.user_id != entry.user_id:
            return None
        try:
            return await self._context_for_session(session)
        except UsersApiError:
            return None

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
        429 locked).  Unknown names are locked out like real ones and take the
        same time (dummy PBKDF2).
        """
        metadata = metadata or SessionMetadata()
        audit_meta = {"remoteAddress": metadata.remote_address}

        user = await self.find_user(username)
        key = self.lockout_key(user, username)
        shown = user.name if user else (sanitize(username, 64) or "")
        user_id = user.id if user else None

        if not self.reserve_attempt(key):
            await self.audit("login_failed", shown, details="account locked",
                             metadata=audit_meta, user_id=user_id)
            raise UsersApiError(429, ACCOUNT_LOCKED)

        if user is None:
            await password_hasher.dummy_verify_async(password)
            await self.audit("login_failed", shown, details="unknown user", metadata=audit_meta)
            raise UsersApiError(401, INVALID_CREDENTIALS)

        if not await self.verify_password(user.id, password):
            await self.audit("login_failed", shown, details="wrong password",
                             metadata=audit_meta, user_id=user_id)
            raise UsersApiError(401, INVALID_CREDENTIALS)

        self.reset_failures(key)

        if user.disabled:
            await self.audit("login_failed", shown, details="account disabled",
                             metadata=audit_meta, user_id=user_id)
            raise UsersApiError(403, ACCOUNT_DISABLED)

        user = await self.user_store.update_user(user.id, last_login_at=now_ms())
        token = self.generate_token()
        session = await self.session_store.create_session(
            user.id, hash_token(token), metadata, user_name=user.name,
        )
        await self.audit(
            "login", user.name, user_id=user.id, actor=user.name, actor_id=user.id,
            metadata={**audit_meta, "sessionId": session.id},
        )
        return LoginResult(
            token=token,
            session=session,
            user=user,
            permissions=await self.user_permissions(user),
        )

    async def check_password_attempt(self, ctx: AuthContext, target: User, password: str) -> bool:
        """Verify a password as a lockout-tracked attempt (verify-password,
        self password change).  Raises 429 when locked; audits failures."""
        key = target.id
        if not self.reserve_attempt(key):
            raise UsersApiError(429, ACCOUNT_LOCKED)
        valid = await self.verify_password(target.id, password)
        if valid:
            self.reset_failures(key)
        else:
            await self.audit(
                "login_failed", target.name,
                details="password verification failed",
                actor=ctx.user.name, actor_id=ctx.user.id, user_id=target.id,
            )
        return valid

    async def logout(self, token: str) -> None:
        session = await self.session_store.get_session(hash_token(token))
        await self.session_store.delete_session(hash_token(token))
        if session:
            self._drop_tickets(session.id)
            user = await self.user_store.get_user(session.user_id)
            name = user.name if user else session.user_name
            await self.audit("logout", name, user_id=session.user_id, actor=name,
                             actor_id=session.user_id, metadata={"sessionId": session.id})

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    async def seed(self, default_admin_password: str | None) -> None:
        """Seed the built-in groups and (unless external auth) the root user.

        Without a configured password a random one is generated, logged once
        and the root account must change it at first login."""
        if not await self.group_store.get_group(ADMIN_GROUP):
            await self.group_store.create_group(
                ADMIN_GROUP, "Administrators", ["*"], built_in=True,
            )
        if not await self.group_store.get_group(USERS_GROUP):
            await self.group_store.create_group(
                USERS_GROUP, "Default group for new accounts",
                list(DEFAULT_USERS_GROUP_PERMISSIONS), built_in=True,
            )
        if self.external_auth:
            return
        if len(await self.user_store.get_users()) != 0:
            return
        generated = default_admin_password is None
        password = self.generate_password() if generated else default_admin_password
        root = await self.user_store.create_user(
            name="root", email="root@localhost", groups=[ADMIN_GROUP],
            must_change_password=generated,
        )
        await self.set_password(root.id, password)
        if generated:
            logger.warning(
                "Users plugin: no default admin password configured. Generated a one-time "
                "password for 'root': %s  (it must be changed at first login)",
                password,
                extra={"qcli_sensitive": True},
            )
        elif password.lower() in WEAK_ADMIN_PASSWORDS:
            logger.warning(
                "Users plugin: the 'root' account uses a well-known default password. "
                "Change it before exposing this server."
            )


__all__ = [
    "AuthContext",
    "AuthService",
    "AuthServiceOptions",
    "LoginResult",
    "covers",
    "hash_token",
    "permission_matches",
]
