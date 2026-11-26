"""The portal plugin's entry point (``qodalis_cli.plugins``, ``builder.add_plugin``): the developer portal site at
``path``, showing the guides of the docs plugin and the API reference of the public-api plugin, however they were
added (code, mount or marketplace: the ``portal.section.docs`` and ``portal.section.api`` services).  It loads after
them (``CLI_PLUGIN_LOAD_ORDER.DEFAULT + 10``); without either it needs configuration.  The path, name and logo are its
settings (provider ``portal-plugin``); branding and colours stay the ``portal`` provider's."""

from __future__ import annotations

from typing import Any

from qodalis_cli_server_abstractions import CLI_PLUGIN_LOAD_ORDER, CliPluginEntryInfo
from qodalis_cli_server_abstractions.config import CliConfigSettingDefinition, CliConfigValidation

from .cli_portal_builder import DEFAULT_NAME, DEFAULT_PATH, CliPortalBuilder

#: The plugin, for ``builder.add_plugin`` (the marketplace's record says it for an installed copy).
plugin = CliPluginEntryInfo(id="portal", package="qodalis-cli-server-plugin-portal", title="Portal", icon="🏠")

#: After the docs and public-api plugins it shows.
order = CLI_PLUGIN_LOAD_ORDER.DEFAULT + 10

#: The id of its settings provider (``portal`` is the provider of its branding).
SETTINGS_ID = "portal-plugin"


def settings() -> list[CliConfigSettingDefinition]:
    """Its settings (code options of the same keys are their defaults; ``theme``, ``footer_links``, ``sections`` and
    ``site_dir`` from code only)."""
    restart = {"requires_restart": True}
    return [
        CliConfigSettingDefinition(key="path", label="Path", description="Where the portal is served (/ for the root)", type="string",
                                   default=DEFAULT_PATH, validation=CliConfigValidation(required=True, pattern=r"^(/|(/[A-Za-z0-9._~-]+)+)$"), **restart),
        CliConfigSettingDefinition(key="name", label="Name", type="string", default=DEFAULT_NAME,
                                   validation=CliConfigValidation(required=True, max=80), **restart),
        CliConfigSettingDefinition(key="logoUrl", label="Logo", description="An http(s) URL or a root-relative path; empty: none",
                                   type="string", default="", validation=CliConfigValidation(max=2000), **restart),
    ]


async def create_plugin(context: Any) -> Any:
    provider = await context.settings(id=SETTINGS_ID, title="Portal (setup)", description="Where the developer portal is served", settings=settings())

    def text(key: str) -> str:
        return str(provider.value("settings", key) or "").strip()

    sections = context.options.get("sections")
    if sections is None:
        sections = [s for s in (context.services.get(f"portal.section.{kind}") for kind in ("docs", "api")) if s is not None]
    if not sections:
        return context.needs_configuration("a docs or public-api plugin (install one, or add it before the portal)")
    builder = CliPortalBuilder().set_path(text("path") or DEFAULT_PATH).set_name(text("name") or DEFAULT_NAME)
    if text("logoUrl"):
        builder.set_logo_url(text("logoUrl"))
    site_dir = context.options.get("site_dir") or context.options.get("siteDir")
    if isinstance(site_dir, str):
        builder.set_site_dir(site_dir)
    theme = context.options.get("theme")
    if isinstance(theme, dict):
        builder.set_theme(theme)
    for link in context.options.get("footer_links") or context.options.get("footerLinks") or []:
        builder.add_footer_link(link["label"], link["url"]) if isinstance(link, dict) else builder.add_footer_link(*link)
    for section in sections:
        builder.add_section(section)
    return builder.build()
