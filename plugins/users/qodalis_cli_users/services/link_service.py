"""Link requests: a terminal (``server add`` in the web CLI) asks to be linked
and a signed-in user approves it in a browser, like the OAuth device flow
(spec: web-cli ``docs/superpowers/specs/2026-11-12-server-linking-design.md``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote

from ..errors import UsersApiError, bad_request, not_found
from ..models import now_ms
from ..validation import validate_permissions
from .auth_service import AuthContext, AuthService

#: Seconds a link request waits for its approval.
LINK_EXPIRES_IN_SECONDS = 600
#: Seconds between two polls of the terminal.
LINK_POLL_INTERVAL_SECONDS = 2
#: Pending requests kept at most; the oldest is dropped when full.
MAX_PENDING_LINKS = 100
#: The approval page of the admin dashboard.
DEFAULT_LINK_APPROVAL_URL = "/qcli/admin/link"
#: Link requests created per client address and minute at most.
LINK_CREATE_RATE_LIMIT = 10
LINK_CREATE_RATE_WINDOW_MS = 60_000

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
MAX_CLIENT_LENGTH = 100
TOKEN_NAME_MAX = 64
#: Numbered names tried when ``link <client>`` is taken.
MAX_NAME_SUFFIX = 20
NOT_FOUND = "link request not found or expired"

_APPROVAL_URL_RE = re.compile(r"^(https?://\S+|/(?![/\\])\S*)$")
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f]")
_TOKEN_NAME_INVALID = re.compile(r"[^A-Za-z0-9._ -]")
_CODE_SEPARATORS = re.compile(r"[\s-]")


def validate_link_approval_url(url: Any) -> str:
    """An http(s) URL or a root-relative path (not ``//host``)."""
    if not isinstance(url, str) or not _APPROVAL_URL_RE.match(url):
        raise ValueError("[users] link_approval_url must be an http(s) URL or a root-relative path")
    return url


def normalize_user_code(code: str) -> str:
    """Upper case without dashes and spaces (``abcd-2345`` → ``ABCD2345``)."""
    return _CODE_SEPARATORS.sub("", code).upper()


def _hash_code(code: str) -> bytes:
    return hashlib.sha256(normalize_user_code(code).encode("utf-8")).digest()


def _format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def link_token_name(client: str, suffix: int = 0) -> str:
    """A token name from a client name: ``link <client>``, ``[A-Za-z0-9._ -]``
    only, at most 64 characters; ``suffix`` n > 0 appends `` n+1``."""
    tail = f" {suffix + 1}" if suffix > 0 else ""
    base = _TOKEN_NAME_INVALID.sub("-", f"link {client}").strip()
    return base[: TOKEN_NAME_MAX - len(tail)].rstrip() + tail


def link_verification_url(approval_url: str, user_code: str) -> str:
    """Where the approval page is: ``{code}`` replaced, otherwise ``?code=``
    (or ``&code=``) appended."""
    code = quote(user_code, safe="")
    if "{code}" in approval_url:
        return approval_url.replace("{code}", code)
    return f"{approval_url}{'&' if '?' in approval_url else '?'}code={code}"


@dataclass
class LinkRequest:
    link_id: str
    #: sha256 of the normalised user code.
    code_hash: bytes
    user_code: str
    client: str
    permissions: Optional[list[str]]
    origin: Optional[str]
    remote_address: Optional[str]
    created_at: int
    expires_at: int
    status: str = "pending"  # pending | approving | approved | denied
    token: Optional[str] = None
    user_name: Optional[str] = None
    token_expires_at: Optional[int] = None


class LinkRateLimiter:
    """At most ``limit`` link requests per client address and window."""

    def __init__(self, limit: int = LINK_CREATE_RATE_LIMIT, window_ms: int = LINK_CREATE_RATE_WINDOW_MS,
                 now: Callable[[], int] = now_ms, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_ms = window_ms
        self._now = now
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[int]] = OrderedDict()

    def try_acquire(self, key: str) -> bool:
        now = self._now()
        window = self._hits.get(key)
        if window is None:
            window = deque()
            self._hits[key] = window
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
        while window and window[0] <= now - self.window_ms:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True

    def sweep(self) -> None:
        cutoff = self._now() - self.window_ms
        for key in [k for k, w in self._hits.items() if not w or w[-1] <= cutoff]:
            del self._hits[key]


class LinkService:
    """Link requests in memory: created by terminals, approved or denied by
    signed-in users.  A restart drops them."""

    def __init__(
        self,
        auth_service: AuthService,
        approval_url: str = DEFAULT_LINK_APPROVAL_URL,
        now: Callable[[], int] = now_ms,
    ) -> None:
        self.auth_service = auth_service
        self.approval_url = approval_url
        self._now = now
        self._links: OrderedDict[str, LinkRequest] = OrderedDict()

    # ``POST /link``
    def create(self, body: Any, origin: str | None = None, remote_address: str | None = None) -> dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        raw_client = body.get("client")
        client = raw_client.strip() if isinstance(raw_client, str) else ""
        if not 1 <= len(client) <= MAX_CLIENT_LENGTH or _CONTROL_RE.search(client):
            raise bad_request(f"client must be 1-{MAX_CLIENT_LENGTH} characters")
        permissions: list[str] | None = None
        value = body.get("permissions")
        if value is not None:
            if (
                not isinstance(value, list) or not value
                or any(not isinstance(p, str) or not p for p in value)
            ):
                raise bad_request("permissions must be a non-empty array of strings")
            permissions = validate_permissions(value)

        self.sweep()
        while len(self._links) >= MAX_PENDING_LINKS:
            self._links.popitem(last=False)

        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        user_code = _format_code(code)
        link_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        now = self._now()
        self._links[link_id] = LinkRequest(
            link_id=link_id,
            code_hash=_hash_code(code),
            user_code=user_code,
            client=client,
            permissions=permissions,
            origin=origin[:200] if origin else None,
            remote_address=remote_address or None,
            created_at=now,
            expires_at=now + LINK_EXPIRES_IN_SECONDS * 1000,
        )
        return {
            "linkId": link_id,
            "userCode": user_code,
            "verificationUrl": link_verification_url(self.approval_url, user_code),
            "expiresIn": LINK_EXPIRES_IN_SECONDS,
            "interval": LINK_POLL_INTERVAL_SECONDS,
        }

    # ``GET /link/{linkId}``: the token is handed out once, then the request is gone.
    def poll(self, link_id: str) -> dict[str, Any] | None:
        self.sweep()
        link = self._links.get(link_id)
        if link is None:
            return None
        if link.status == "approved":
            del self._links[link_id]
            return {
                "status": "approved",
                "token": link.token,
                "userName": link.user_name,
                "expiresAt": link.token_expires_at,
            }
        if link.status == "denied":
            del self._links[link_id]
            return {"status": "denied"}
        return {"status": "pending"}

    def _find_pending(self, user_code: str) -> LinkRequest | None:
        """The pending request with this user code (constant-time compare)."""
        self.sweep()
        code_hash = _hash_code(user_code)
        found: LinkRequest | None = None
        for link in self._links.values():
            if hmac.compare_digest(link.code_hash, code_hash) and link.status == "pending":
                found = link
        return found

    # ``GET /link/code/{userCode}``
    def view(self, user_code: str) -> dict[str, Any]:
        link = self._find_pending(user_code)
        if link is None:
            raise not_found(NOT_FOUND)
        return {
            "userCode": link.user_code,
            "client": link.client,
            "origin": link.origin,
            "remoteAddress": link.remote_address,
            "permissions": link.permissions,
            "createdAt": link.created_at,
            "expiresAt": link.expires_at,
        }

    # ``POST /link/code/{userCode}/approve``
    async def approve(self, ctx: AuthContext, user_code: str) -> dict[str, str]:
        """Create the approver's API token for the terminal, through the same
        path as ``POST /tokens`` (session only, permissions the approver's,
        token limit, default expiry, ``token_created`` audit)."""
        # Imported here: the tokens controller imports the services package.
        from ..controllers.tokens_controller import (
            create_api_token,
            default_token_expiry,
            ensure_can_create_tokens,
            revoke_api_token,
            scope_permissions,
        )

        link = self._find_pending(user_code)
        if link is None:
            raise not_found(NOT_FOUND)
        # Adapter identities approve only when the host allows API tokens.
        ensure_can_create_tokens(
            ctx, bool(ctx.external) and self.auth_service.options.api_tokens_with_external_auth,
        )
        permissions = scope_permissions(ctx, link.permissions)
        # Claimed before the token exists, so a parallel approval or deny gets 404
        # instead of leaving a second token behind.
        link.status = "approving"
        created = None
        suffix = 0
        try:
            while created is None:
                try:
                    created = await create_api_token(
                        self.auth_service, ctx, link_token_name(link.client, suffix),
                        permissions, default_token_expiry(),
                    )
                except UsersApiError as exc:
                    if exc.status_code != 409 or suffix >= MAX_NAME_SUFFIX:
                        raise
                    suffix += 1
        except BaseException:
            # Another approver (with the permissions, or a free token slot) can still approve it.
            link.status = "pending"
            raise
        token, record = created
        # Expired (swept or not) or dropped while the token was created: the
        # terminal can no longer collect it, so the token goes too.
        self.sweep()
        if self._links.get(link.link_id) is not link:
            await revoke_api_token(self.auth_service, ctx, record)
            raise not_found(NOT_FOUND)
        link.status = "approved"
        link.token = token
        link.user_name = ctx.user.name
        link.token_expires_at = record.expires_at
        metadata: dict[str, Any] = {"client": link.client}
        if link.origin:
            metadata["origin"] = link.origin
        metadata["tokenId"] = record.id
        await self.auth_service.audit(
            "link_approved", ctx.user.name,
            details=f"{link.client} ({record.name})",
            actor=ctx.user.name, actor_id=ctx.user.id, user_id=ctx.user.id,
            metadata=metadata,
        )
        return {"tokenId": record.id, "tokenName": record.name}

    # ``POST /link/code/{userCode}/deny``
    async def deny(self, ctx: AuthContext, user_code: str) -> None:
        link = self._find_pending(user_code)
        if link is None:
            raise not_found(NOT_FOUND)
        link.status = "denied"
        metadata: dict[str, Any] = {"client": link.client}
        if link.origin:
            metadata["origin"] = link.origin
        await self.auth_service.audit(
            "link_denied", ctx.user.name,
            details=link.client,
            actor=ctx.user.name, actor_id=ctx.user.id, user_id=ctx.user.id,
            metadata=metadata,
        )

    def sweep(self) -> None:
        """Drop expired requests (an approved token not collected in time
        stays listed with the user's tokens)."""
        now = self._now()
        for link_id in [i for i, link in self._links.items() if link.expires_at <= now]:
            del self._links[link_id]
