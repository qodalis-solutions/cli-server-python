"""Admin configuration — reads credentials from environment variables."""

from __future__ import annotations

import hmac
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Any
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
    sign_in_url: str | None = None

    # Mutable runtime settings
    _mutable_settings: dict[str, Any] = field(default_factory=dict)

    FORBIDDEN_KEYS: frozenset[str] = frozenset(
        {"username", "password", "jwt_secret", "jwtSecret"}
    )

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

    def get_config_sections(self) -> list[dict[str, Any]]:
        """Return structured configuration sections for the admin API."""
        import platform as _platform
        import sys as _sys

        custom_entries = [
            {
                "key": k,
                "value": v,
                "type": _infer_type(v),
                "description": "",
                "mutable": True,
            }
            for k, v in self._mutable_settings.items()
        ]

        return [
            {
                "name": "server",
                "mutable": False,
                "settings": [
                    {"key": "platform", "value": "python", "type": "string", "description": "Server platform", "mutable": False},
                    {"key": "platformVersion", "value": _sys.version.split()[0], "type": "string", "description": "Python version", "mutable": False},
                    {"key": "os", "value": _platform.platform(), "type": "string", "description": "Operating system", "mutable": False},
                ],
            },
            {
                "name": "auth",
                "mutable": False,
                "settings": [
                    {"key": "username", "value": self.username, "type": "string", "description": "Admin username", "mutable": False},
                    {"key": "jwtSecretConfigured", "value": bool(self.jwt_secret), "type": "boolean", "description": "Whether JWT secret is explicitly set", "mutable": False},
                ],
            },
            {
                "name": "custom",
                "mutable": True,
                "settings": custom_entries,
            },
        ]

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Update mutable runtime settings.

        Keys that overlap with sensitive configuration are silently rejected.
        """
        sanitized = {k: v for k, v in settings.items() if k not in self.FORBIDDEN_KEYS}
        self._mutable_settings.update(sanitized)

    def get_settings(self) -> dict[str, Any]:
        """Return a copy of the current mutable runtime settings."""
        return dict(self._mutable_settings)


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


def _infer_type(val: Any) -> str:
    """Infer a JSON-schema-style type string from a Python value."""
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, (int, float)):
        return "number"
    if isinstance(val, list):
        return "string[]"
    return "string"
