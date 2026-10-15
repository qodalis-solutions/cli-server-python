"""Remote shell security (Appendix C): options, access modes and helpers.

The PTY shell gives OS-level access, so it is off unless enabled explicitly
(``CliServerOptions(enable_shell=True)``) and, once enabled, every connection
must pass exactly one access mode:

``users``
    The users plugin is mounted: a ticket-authenticated session holding
    ``shell:execute``.  ``token`` / ``authorize``, when also configured, must
    pass as well.
``token``
    ``CliShellOptions(token=...)``: the client sends
    ``Authorization: Bearer <token>`` (header, or the ``_header_Authorization``
    query parameter browser clients use).  Constant-time comparison.
``custom``
    ``CliShellOptions(authorize=callable)``: a host-defined check of the
    WebSocket (headers, query, client address).  Exceptions deny.
``anonymous``
    Only with ``CliShellOptions(allow_anonymous=True)`` (logs a warning).
``locked``
    Enabled but none of the above: every connection is refused (4401).
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Union

logger = logging.getLogger(__name__)

SHELL_PERMISSION = "shell:execute"

# Close codes (C5) — sent after accepting the upgrade so browsers can read them.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_DISABLED = 4404
CLOSE_TIMEOUT = 4408
CLOSE_LIMIT = 4429

REASON_LOGIN_REQUIRED = "login required"
REASON_SESSION_ENDED = "session ended"
REASON_FORBIDDEN = "forbidden"
REASON_DISABLED = "shell disabled"
REASON_LOCKED = "shell access is not configured"
REASON_TIMEOUT = "shell timed out"
REASON_LIMIT = "too many shell sessions"

MODE_USERS = "users"
MODE_TOKEN = "token"
MODE_CUSTOM = "custom"
MODE_ANONYMOUS = "anonymous"
MODE_LOCKED = "locked"

#: Variables always copied from the server environment into the shell.
BASE_ENV_NAMES = ("PATH", "HOME", "USER", "LANG")

#: Upper bound for the periodic session re-check (C2: at least every 60 s).
MAX_SESSION_CHECK_INTERVAL = 60.0
DEFAULT_SESSION_CHECK_INTERVAL = 30.0

# ``shell_closed`` audit ``metadata.reason`` values.
END_EXITED = "exited"
END_CLOSED = "closed"
END_SESSION_ENDED = "session ended"
END_PERMISSION_REMOVED = "permission removed"
END_TIMED_OUT = "shell timed out"
END_SHUTDOWN = "server shutdown"
END_ERROR = "error"

#: Stdin touches the owning session at most this often (seconds).
TOUCH_INTERVAL = 30.0

AuthorizeFn = Callable[[Any], Union[bool, Awaitable[bool]]]


@dataclass
class CliShellOptions:
    """Security and runtime options of the remote shell.

    Times are in seconds.  ``None`` (or ``0``) disables a timeout.
    """

    #: Shared secret: clients send ``Authorization: Bearer <token>``.
    token: str | None = field(default=None, repr=False)
    #: ``callable(websocket) -> bool | Awaitable[bool]``; exceptions deny.
    authorize: AuthorizeFn | None = None
    #: Open the shell without authentication (only when nothing else is
    #: configured; logs a warning at startup).
    allow_anonymous: bool = False
    #: Concurrent shells per user (users mode) or per remote address.
    max_sessions_per_user: int = 3
    #: Concurrent shells in total.
    max_sessions: int = 20
    #: Close the shell after this long without stdin (default 15 min).
    idle_timeout: float | None = 15 * 60
    #: Close the shell after this long regardless of activity.
    max_duration: float | None = None
    #: Working directory of the shell (default: the server's).
    cwd: str | None = None
    #: Shell executable (default: detected).
    shell: str | None = None
    #: Extra server environment variable names copied into the shell.
    env_allow: list[str] = field(default_factory=list)
    #: Variables set in the shell environment (override everything else).
    env_set: dict[str, str] = field(default_factory=dict)
    #: Users mode: how often an open shell re-checks its session and
    #: ``shell:execute`` (default 30 s, capped at 60 s; also re-checked
    #: immediately on session/user/group changes).
    session_check_interval: float = DEFAULT_SESSION_CHECK_INTERVAL
    #: Trusted reverse-proxy hops for the client address used by per-address
    #: limits and logs: ``False``/``0`` uses the socket peer (the header is
    #: ignored), ``True`` = 1 hop, an int ``n`` = ``n`` hops.  The client is
    #: the ``X-Forwarded-For`` entry at ``len - n`` (the rightmost for one
    #: proxy); only enable behind trusted reverse proxies.
    trust_proxy: bool | int = False


@dataclass
class ShellPrincipal:
    """The user an open shell belongs to (users mode)."""

    user_id: str
    user_name: str
    session_id: str | None = None
    #: Provider-specific context (e.g. the users plugin ``AuthContext``).
    context: Any = None


class ShellAccessProvider(Protocol):
    """Implemented by an auth plugin (the users plugin) to own shell access.

    Registered with ``CliServerResult.set_shell_access_provider`` (the users
    plugin does this in ``on_mount``); its presence selects ``users`` mode.
    """

    async def resolve(self, websocket: Any) -> ShellPrincipal | None:
        """The authenticated caller, or ``None`` (close 4401)."""

    def can_open(self, principal: ShellPrincipal) -> bool:
        """Whether the caller may open a shell (``False`` closes 4403)."""

    async def is_active(self, principal: ShellPrincipal) -> bool | str:
        """``True`` while the caller's session is alive and allowed; ``False``
        (audit reason ``session ended``) or a ``shell_closed`` audit reason
        string such as ``"permission removed"`` otherwise.  Must not extend
        the session by itself."""

    async def touch(self, principal: ShellPrincipal) -> None:
        """Optional: record session activity (called after a successful
        re-check when stdin arrived since the previous one)."""

    async def audit(self, event: str, principal: ShellPrincipal, metadata: dict[str, Any]) -> None:
        """Record ``shell_opened`` / ``shell_closed``."""

    def add_change_listener(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` whenever sessions, users or groups change."""


class ShellRejected(Exception):
    """A connection refused with a WebSocket close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def resolve_mode(options: CliShellOptions, has_provider: bool) -> str:
    """The access mode for the configured options (C1 precedence)."""
    if has_provider:
        return MODE_USERS
    if options.token:
        return MODE_TOKEN
    if options.authorize is not None:
        return MODE_CUSTOM
    if options.allow_anonymous:
        return MODE_ANONYMOUS
    return MODE_LOCKED


def presented_token(websocket: Any) -> str | None:
    """The bearer token from the ``Authorization`` header or the
    ``_header_Authorization`` query parameter (browser clients)."""
    value = websocket.headers.get("authorization")
    if not value:
        value = websocket.query_params.get("_header_Authorization")
    if not value:
        return None
    value = value.strip()
    if value[:7].lower() != "bearer ":
        return None
    return value[7:].strip() or None


def tokens_equal(presented: str, expected: str) -> bool:
    """Constant-time comparison of SHA-256 digests (no length leak)."""
    return hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def check_token(websocket: Any, expected: str) -> None:
    """Raise :class:`ShellRejected` unless the caller presents ``expected``."""
    presented = presented_token(websocket)
    if presented is None:
        raise ShellRejected(CLOSE_UNAUTHORIZED, REASON_LOGIN_REQUIRED)
    if not tokens_equal(presented, expected):
        raise ShellRejected(CLOSE_FORBIDDEN, REASON_FORBIDDEN)


async def check_authorize(websocket: Any, authorize: AuthorizeFn) -> None:
    """Run a host ``authorize`` callback; ``False`` or an exception denies."""
    try:
        result = authorize(websocket)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # deny on any failure
        logger.warning("Shell authorize callback failed (%s); connection denied", type(exc).__name__)
        raise ShellRejected(CLOSE_FORBIDDEN, REASON_FORBIDDEN) from None
    if not result:
        raise ShellRejected(CLOSE_FORBIDDEN, REASON_FORBIDDEN)


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


def remote_address(websocket: Any, trust_proxy: bool | int | None = False) -> str:
    forwarded = forwarded_client_ip(websocket.headers, trust_proxy)
    if forwarded:
        return forwarded
    client = getattr(websocket, "client", None)
    return (client.host if client else None) or "unknown"


__all__ = [
    "CLOSE_DISABLED",
    "CLOSE_FORBIDDEN",
    "CLOSE_LIMIT",
    "CLOSE_TIMEOUT",
    "CLOSE_UNAUTHORIZED",
    "CliShellOptions",
    "SHELL_PERMISSION",
    "ShellAccessProvider",
    "ShellPrincipal",
    "ShellRejected",
    "check_authorize",
    "check_token",
    "forwarded_client_ip",
    "presented_token",
    "resolve_mode",
    "trusted_hops",
]
