"""Admin configuration — reads credentials from environment variables."""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field
from typing import Any

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

        if self.username == "admin" and self.password == "admin":
            logger.warning(
                "Using default admin credentials. Set QCLI_ADMIN_USERNAME"
                " and QCLI_ADMIN_PASSWORD environment variables."
            )

    def validate_credentials(self, username: str, password: str) -> bool:
        """Return *True* if the supplied credentials match."""
        username_ok = hmac.compare_digest(username, self.username)
        password_ok = hmac.compare_digest(password, self.password)
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


def _infer_type(val: Any) -> str:
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, (int, float)):
        return "number"
    if isinstance(val, list):
        return "string[]"
    return "string"
