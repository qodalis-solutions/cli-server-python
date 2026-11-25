"""Admin configuration: the credentials, JWT secret and sign-in URL, read
from environment variables.  The dashboard's settings are served by the
config providers (``auth`` here, the server's built-in ones, and the
plugins')."""

from __future__ import annotations

import hmac
import logging
import os
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("qcli-admin")


@dataclass
class AdminConfig:
    """Holds admin dashboard configuration.

    Credentials default to ``QCLI_ADMIN_USERNAME`` / ``QCLI_ADMIN_PASSWORD``
    environment variables, falling back to ``admin`` / ``admin``.
    """

    username: str = ""
    password: str = ""
    jwt_secret: str = ""
    warn_default: bool = True
    #: Where the host application signs users in, shown by the dashboard in
    #: external sign-in mode and returned by ``GET /api/v1/qcli/auth/mode``.
    #: An absolute ``http(s)`` URL or a root-relative path (``/sign-in``).
    #: ``None``: the dashboard tells users to sign in through the host app.
    #: The dashboard sends users back: it fills ``{returnTo}`` in the URL, or
    #: appends ``returnTo=``, with the dashboard page they were on (e.g. a
    #: terminal link's approval page); after signing in, the host opens that
    #: URL with ``#token=<bearer token>``.
    sign_in_url: str | None = None

    def __post_init__(self) -> None:
        if not self.username:
            self.username = os.environ.get("QCLI_ADMIN_USERNAME", "admin")
        if not self.password:
            self.password = os.environ.get("QCLI_ADMIN_PASSWORD", "admin")
        if not self.jwt_secret:
            self.jwt_secret = os.environ.get("QCLI_ADMIN_JWT_SECRET", "")

        if self.warn_default and self.username == "admin" and self.password == "admin":
            logger.warning(
                "Using default admin credentials. Set QCLI_ADMIN_USERNAME"
                " and QCLI_ADMIN_PASSWORD environment variables."
            )

    def validate(self) -> None:
        """Validate the options set by the host; raises :class:`ValueError`
        on an invalid :attr:`sign_in_url`."""
        if self.sign_in_url is not None and not is_valid_sign_in_url(self.sign_in_url):
            raise ValueError(
                "AdminConfig.sign_in_url must be an absolute http(s) URL or a root-relative path, "
                f"got {self.sign_in_url!r}."
            )

    def validate_credentials(self, username: str, password: str) -> bool:
        """Return *True* if the supplied credentials match."""
        # surrogatepass: a lone surrogate in the input must not raise (500)
        username_ok = hmac.compare_digest(
            username.encode("utf-8", "surrogatepass"), self.username.encode("utf-8", "surrogatepass"),
        )
        password_ok = hmac.compare_digest(
            password.encode("utf-8", "surrogatepass"), self.password.encode("utf-8", "surrogatepass"),
        )
        return username_ok and password_ok

    def uses_default_credentials(self) -> bool:
        """True while the well-known ``admin`` / ``admin`` credentials are in effect."""
        return self.username == "admin" and self.password == "admin"


def is_valid_sign_in_url(value: str | None) -> bool:
    """True for an absolute ``http``/``https`` URL with a host, or a
    root-relative path (``/sign-in``; not protocol-relative ``//host`` or
    ``/\\host``).  Rejects other schemes such as ``javascript:``, and control
    characters or whitespace."""
    if not value or not isinstance(value, str):
        return False
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in value):
        return False
    if value.startswith("/"):
        return len(value) == 1 or value[1] not in "/\\"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  (raises ValueError on an invalid port)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)
