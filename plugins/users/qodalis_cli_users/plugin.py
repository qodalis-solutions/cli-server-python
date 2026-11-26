"""The users plugin's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``): sign-in, users, groups,
permissions, sessions, API tokens and the audit log.  It loads first (``CLI_PLUGIN_LOAD_ORDER.AUTH``) and the server
loads its plugins before it serves, so it protects every route and socket from the first request.  Its stores are
the ``users.stores`` service when a store plugin loaded before it, else in memory.  The setup is its settings
(provider ``users-plugin``; sessions, lockout and passwords stay the ``users`` provider's); code options of other keys
call the builder's setter of that name (``auth_adapter`` / ``authAdapter`` -> ``set_auth_adapter``, ``user_store``,
``allowed_origins``, ...).  Shared as the ``users`` service (the public-api plugin builds on it)."""

from __future__ import annotations

import re
from typing import Any

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .cli_users_builder import CliUsersBuilder

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="users", package="qodalis-cli-server-plugin-users", title="Users", icon="👥")

#: First: the sign-in protects what loads after it.
order = CLI_PLUGIN_LOAD_ORDER.AUTH

#: The id of its settings provider (``users`` is the provider of its sessions and passwords).
SETTINGS_ID = "users-plugin"

#: The stores a store plugin shares (``users.stores``): the builder's setter of each.
_SHARED_STORES = ("user_store", "group_store", "credential_store", "session_store", "api_token_store", "audit_store")


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="defaultAdminPassword", label="root password",
                                   description="The seeded root user's password; empty: a random one, logged once", type="secret", **restart),
        CliConfigSettingDefinition(key="protectServer", label="Protect the server", description="Every route and socket needs a sign-in",
                                   type="boolean", default=True, **restart),
        CliConfigSettingDefinition(key="allowNonExpiringTokens", label="Tokens without expiry",
                                   description="API tokens may be created without an expiry date", type="boolean", default=False, **restart),
        CliConfigSettingDefinition(key="linkApprovalUrl", label="Link approval page",
                                   description="Where users approve a terminal's link request; empty: the dashboard's", type="string",
                                   default="", validation=CliConfigValidation(pattern=r"^(|https?://\S+|/(?![/\\])\S*)$"), **restart),
    ]


def _snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def apply_code_options(builder: Any, options: dict[str, Any], skip: set[str]) -> None:
    """Calls ``builder.set_<option>(value)`` for every code option that is not a setting; an unknown one raises."""
    for key, value in options.items():
        if key in skip:
            continue
        setter = getattr(builder, f"set_{_snake(key)}", None)
        if not callable(setter):
            raise ValueError(f"unknown option of the users plugin: {key}")
        setter(value)


async def create_plugin(context: Any) -> Any:
    defs = settings()
    provider = await context.settings(
        id=SETTINGS_ID, title="Users (setup)", description="How the users plugin signs in and protects the server", settings=defs,
    )

    def value(key: str) -> Any:
        return provider.value("settings", key)

    def text(key: str) -> str:
        return str(value(key) or "").strip()

    builder = CliUsersBuilder()
    # The code's other options (adapters, stores, origins), then the stores a store plugin shares, then the settings
    apply_code_options(builder, context.options, {s.key for s in defs})
    shared = context.services.get("users.stores") if context.services is not None else None
    if isinstance(shared, dict) and not any(_snake(k) == "user_store" for k in context.options):
        for name in _SHARED_STORES:
            if shared.get(name) is not None:
                getattr(builder, f"set_{name}")(shared[name])
    builder.set_protect_server(value("protectServer") is not False)
    builder.set_allow_non_expiring_tokens(value("allowNonExpiringTokens") is True)
    if text("defaultAdminPassword"):
        builder.set_default_admin_password(text("defaultAdminPassword"))
    if text("linkApprovalUrl"):
        builder.set_link_approval_url(text("linkApprovalUrl"))
    users = builder.build(event_bus=context.event_bus)
    await users.startup()
    if context.restart is not None and hasattr(context.restart, "on_shutdown"):
        context.restart.on_shutdown("users plugin", users.shutdown)
    if context.services is not None:
        context.services.set("users", users)
    return users
