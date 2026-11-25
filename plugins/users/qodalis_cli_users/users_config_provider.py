"""``users``: sessions, lockout and passwords, changeable at runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
    format_cli_config_duration,
    parse_cli_config_duration,
)

_MINUTE = 60_000
_DAY = 86_400_000


@dataclass
class UsersConfigFacts:
    """The read-only facts the ``users`` provider shows."""

    protect_server: bool
    #: ``users``, ``external`` or ``guard-only``.
    sign_in: str
    allow_non_expiring_tokens: bool
    api_tokens_with_external_auth: bool
    users_read_only: bool
    groups_read_only: bool


def _whole(value: Any) -> str | None:
    return None if isinstance(value, int) or (isinstance(value, float) and value.is_integer()) else "must be a whole number"


def create_users_config_provider(auth: Any, facts: UsersConfigFacts) -> CliStoredConfigProvider:
    """``users``: sessions, lockout and passwords, changeable at runtime (read
    on every sign-in, request and password change: ``auth.options``); how the
    plugin protects the server is read-only."""
    options = auth.options
    initial = {
        "sessionTimeout": format_cli_config_duration(options.session_timeout_ms),
        "maxSessionLifetime": format_cli_config_duration(options.max_session_lifetime_ms),
        "maxLoginAttempts": options.max_login_attempts,
        "lockout": format_cli_config_duration(options.lockout_ms),
        "minPasswordLength": options.min_password_length,
    }

    def duration(attr: str, key: str) -> Callable[[Any], None]:
        def apply(value: Any) -> None:
            ms = parse_cli_config_duration(value) if isinstance(value, str) else None
            setattr(options, attr, ms if ms is not None else parse_cli_config_duration(initial[key]))
        return apply

    def number(attr: str, key: str) -> Callable[[Any], None]:
        def apply(value: Any) -> None:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            setattr(options, attr, int(value) if ok else initial[key])
        return apply

    def read_only(key: str, label: str, description: str, value: Any) -> CliConfigSettingDefinition:
        return CliConfigSettingDefinition(
            key=key, label=label, description=description,
            type="boolean" if isinstance(value, bool) else "string",
            mutable=False, requires_restart=True, default=value,
        )

    return CliStoredConfigProvider(
        id="users",
        title="Users",
        description="Sign-in sessions, lockout and passwords of the users plugin",
        order=70,
        sections=[
            CliConfigSectionDefinition(id="sessions", title="Sessions", settings=[
                CliConfigSettingDefinition(
                    key="sessionTimeout", label="Idle timeout",
                    description="A session without activity for this long ends",
                    type="duration", default=initial["sessionTimeout"],
                    validation=CliConfigValidation(min=_MINUTE, max=30 * _DAY),
                    apply=duration("session_timeout_ms", "sessionTimeout"),
                ),
                CliConfigSettingDefinition(
                    key="maxSessionLifetime", label="Maximum lifetime",
                    description="A session ends this long after sign-in, whatever its activity",
                    type="duration", default=initial["maxSessionLifetime"],
                    validation=CliConfigValidation(min=_MINUTE, max=365 * _DAY),
                    apply=duration("max_session_lifetime_ms", "maxSessionLifetime"),
                ),
            ]),
            CliConfigSectionDefinition(id="security", title="Lockout and passwords", settings=[
                CliConfigSettingDefinition(
                    key="maxLoginAttempts", label="Failed sign-ins before lockout",
                    description="Consecutive failures that lock an account",
                    type="number", default=initial["maxLoginAttempts"],
                    validation=CliConfigValidation(min=1, max=100), validate=_whole,
                    apply=number("max_login_attempts", "maxLoginAttempts"),
                ),
                CliConfigSettingDefinition(
                    key="lockout", label="Lockout",
                    description="How long a locked account stays locked (also the window failures are counted in)",
                    type="duration", default=initial["lockout"],
                    validation=CliConfigValidation(min=1000, max=_DAY),
                    apply=duration("lockout_ms", "lockout"),
                ),
                CliConfigSettingDefinition(
                    key="minPasswordLength", label="Minimum password length",
                    description="Applies to new passwords",
                    type="number", default=initial["minPasswordLength"],
                    validation=CliConfigValidation(min=1, max=1024), validate=_whole,
                    apply=number("min_password_length", "minPasswordLength"),
                ),
            ]),
            CliConfigSectionDefinition(id="access", title="Access", description="Set in code", settings=[
                read_only(
                    "protectServer", "Protects the server",
                    "Every route and socket needs a users session", facts.protect_server,
                ),
                read_only(
                    "signIn", "Sign-in",
                    "users: the plugin's accounts; external: the host's auth adapter; guard-only: tokens only",
                    facts.sign_in,
                ),
                read_only(
                    "allowNonExpiringTokens", "Non-expiring tokens",
                    "API tokens may never expire", facts.allow_non_expiring_tokens,
                ),
                read_only(
                    "apiTokensWithExternalAuth", "Tokens with external sign-in",
                    "API tokens accepted beside the host's adapter", facts.api_tokens_with_external_auth,
                ),
                read_only("usersReadOnly", "Users managed by the host", "User writes answer 501", facts.users_read_only),
                read_only(
                    "groupsReadOnly", "Groups managed by the host", "Group writes answer 501", facts.groups_read_only,
                ),
            ]),
        ],
    )
