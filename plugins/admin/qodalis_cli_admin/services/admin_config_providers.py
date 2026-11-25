"""The admin plugin's config providers: ``auth`` (read-only) and ``logs``."""

from __future__ import annotations

import os
from typing import Any, Callable

from qodalis_cli_server_abstractions.config import (
    CliConfigEntry,
    CliConfigProviderStorage,
    CliConfigSection,
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
    ICliConfigProvider,
)

from .admin_config import AdminConfig
from .log_ring_buffer import LOG_BUFFER_MAX_CAPACITY, LOG_BUFFER_MIN_CAPACITY, LOG_LEVELS, LogRingBuffer

#: How the dashboard signs users in: ``password``, ``users`` or ``external``.
ADMIN_SIGN_IN_MODES = ["password", "users", "external"]


class AdminAuthConfigProvider(ICliConfigProvider):
    """``auth`` (read-only): how the admin dashboard signs users in: the mode,
    the static admin's username, whether the default credentials or an
    explicit JWT secret are in use, and the host's sign-in URL.  Never a
    password or the secret itself."""

    id = "auth"
    title = "Authentication"
    description = "How the admin dashboard signs users in (read-only)"
    order = 10
    storage = CliConfigProviderStorage(kind="options", description="Read from the admin plugin's options", sources=[])

    def __init__(self, config: AdminConfig, mode: Callable[[], str]) -> None:
        self._config = config
        self._mode = mode

    def get_sections(self, context: Any = None) -> list[CliConfigSection]:
        mode = self._mode()
        config = self._config
        return [CliConfigSection(id="admin", title="Admin dashboard", entries=[
            CliConfigEntry(
                key="mode", label="Sign-in mode", type="enum", value=mode,
                description=(
                    "password: the static admin credentials; users: the users plugin's accounts; "
                    "external: the host application signs users in"
                ),
                validation=CliConfigValidation(options=list(ADMIN_SIGN_IN_MODES)),
            ),
            CliConfigEntry(
                key="username", label="Admin username", description="The static admin (password mode)",
                type="string", value=config.username,
                source="environment" if os.environ.get("QCLI_ADMIN_USERNAME") is not None else "default",
            ),
            CliConfigEntry(
                key="defaultCredentials", label="Default credentials",
                description="The well-known admin / admin credentials are in effect (password mode)",
                type="boolean", value=mode == "password" and config.uses_default_credentials(),
            ),
            CliConfigEntry(
                key="jwtSecretConfigured", label="JWT secret set",
                description="Whether JWT secret is explicitly set (otherwise random per start)",
                type="boolean", value=bool(config.jwt_secret),
                source="environment" if os.environ.get("QCLI_ADMIN_JWT_SECRET") is not None else "default",
            ),
            CliConfigEntry(
                key="signInUrl", label="Sign-in URL", description="Where the host application signs users in (external mode)",
                type="string", value=config.sign_in_url,
            ),
        ])]


def create_logs_config_provider(buffer: LogRingBuffer) -> CliStoredConfigProvider:
    """``logs``: the admin log buffer (what the dashboard's Logs page and the
    logs socket show), changeable at runtime."""
    initial_capacity = buffer.capacity
    initial_level = buffer.min_level

    def whole(value: Any) -> str | None:
        return None if isinstance(value, int) or (isinstance(value, float) and value.is_integer()) else "must be a whole number"

    def apply_capacity(value: Any) -> None:
        buffer.resize(int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else initial_capacity)

    def apply_level(value: Any) -> None:
        buffer.min_level = value if isinstance(value, str) else initial_level

    return CliStoredConfigProvider(
        id="logs",
        title="Logs",
        description="The log buffer of the admin dashboard",
        order=80,
        sections=[CliConfigSectionDefinition(id="buffer", title="Log buffer", settings=[
            CliConfigSettingDefinition(
                key="capacity", label="Entries kept",
                description="How many log entries the server keeps in memory (the newest)",
                type="number", default=initial_capacity,
                validation=CliConfigValidation(min=LOG_BUFFER_MIN_CAPACITY, max=LOG_BUFFER_MAX_CAPACITY),
                validate=whole, apply=apply_capacity,
            ),
            CliConfigSettingDefinition(
                key="minLevel", label="Lowest level",
                description="Entries below this level are neither kept nor streamed",
                type="enum", default=initial_level,
                validation=CliConfigValidation(options=list(LOG_LEVELS)),
                apply=apply_level,
            ),
        ])],
    )
