"""Admin configuration — reads credentials from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


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

    def __post_init__(self) -> None:
        if not self.username:
            self.username = os.environ.get("QCLI_ADMIN_USERNAME", "admin")
        if not self.password:
            self.password = os.environ.get("QCLI_ADMIN_PASSWORD", "admin")
        if not self.jwt_secret:
            self.jwt_secret = os.environ.get("QCLI_ADMIN_JWT_SECRET", "")

    def validate_credentials(self, username: str, password: str) -> bool:
        """Return *True* if the supplied credentials match."""
        return username == self.username and password == self.password

    def get_config_sections(self) -> list[dict[str, Any]]:
        """Return structured configuration sections for the admin API."""
        return [
            {
                "id": "auth",
                "label": "Authentication",
                "settings": {
                    "username": self.username,
                    "jwtSecretConfigured": bool(self.jwt_secret),
                },
            },
            {
                "id": "runtime",
                "label": "Runtime",
                "settings": dict(self._mutable_settings),
            },
        ]

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Update mutable runtime settings."""
        self._mutable_settings.update(settings)

    def get_settings(self) -> dict[str, Any]:
        """Return a copy of the current mutable runtime settings."""
        return dict(self._mutable_settings)
