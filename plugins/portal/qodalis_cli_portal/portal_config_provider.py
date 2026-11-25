"""The ``portal`` config provider: the portal's branding, changeable at runtime."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
)

from .options import MAX_FOOTER_LINKS, validate_footer_links, validate_logo, validate_name

_LINKS_SHAPE = 'must be a list of { "label": ..., "url": ... } links'


@dataclass
class PortalBranding:
    """The portal's branding that ``config set portal.branding.<key>`` changes (the theme colours stay in code)."""

    name: str
    #: ``None``: no logo.
    logo: Optional[str]
    theme_mode: str
    footer_links: list[dict[str, str]] = field(default_factory=list)

    def clone(self) -> "PortalBranding":
        return PortalBranding(self.name, self.logo, self.theme_mode, copy.deepcopy(self.footer_links))


@dataclass
class PortalSiteFacts:
    """What the read-only ``site`` section shows."""

    #: The mount (``/qcli/portal``).
    path: str
    site_found: bool
    #: The sections added (``docs``, ``api``), in the order added.
    sections: list[str] = field(default_factory=list)


def _message(check: Callable[[], Any]) -> Optional[str]:
    try:
        check()
        return None
    except ValueError as exc:
        return str(exc)


def _links(value: list[Any]) -> list[dict[str, str]]:
    return validate_footer_links([(link.get("label"), link.get("url")) for link in value])


def footer_links_error(value: Any) -> Optional[str]:
    """The rule of a new ``footerLinks`` value: a list of at most 20 ``{label, url}`` links, as
    ``add_footer_link`` checks them."""
    if not isinstance(value, list):
        return _LINKS_SHAPE
    if len(value) > MAX_FOOTER_LINKS:
        return f"must have at most {MAX_FOOTER_LINKS} links"
    if any(not isinstance(link, dict) for link in value):
        return _LINKS_SHAPE
    return _message(lambda: _links(value))


def create_portal_config_provider(
    defaults: PortalBranding,
    apply: Callable[[PortalBranding], None],
    site: PortalSiteFacts,
) -> CliStoredConfigProvider:
    """``portal``: the branding (``branding``: name, logo, theme mode, footer links), changeable at
    runtime; the builder's values are the defaults and every change is in the next ``/portal.json``
    (``apply`` receives the current branding).  Where the portal is mounted and whether its site was
    found are read-only (``site``)."""
    current = defaults.clone()

    def setter(update: Callable[[Any], None]) -> Callable[[Any], None]:
        def run(value: Any) -> None:
            update(value)
            apply(current.clone())

        return run

    def set_name(value: Any) -> None:
        current.name = value if isinstance(value, str) else defaults.name

    def set_logo(value: Any) -> None:
        current.logo = value if isinstance(value, str) and value != "" else None

    def set_mode(value: Any) -> None:
        current.theme_mode = value if isinstance(value, str) else defaults.theme_mode

    def set_links(value: Any) -> None:
        current.footer_links = _links(value) if isinstance(value, list) else copy.deepcopy(defaults.footer_links)

    return CliStoredConfigProvider(
        id="portal",
        title="Portal",
        description="Branding of the documentation portal (/portal.json)",
        order=80,
        sections=[
            CliConfigSectionDefinition(
                id="branding",
                title="Branding",
                description=(
                    "Shown by the portal site; a change is in the next /portal.json, no restart. "
                    "The theme colours are set in code (set_theme)."
                ),
                settings=[
                    CliConfigSettingDefinition(
                        key="name", label="Name", description="The site name, in the header and the page title",
                        type="string", default=defaults.name, validation=CliConfigValidation(min=1, max=80),
                        validate=lambda value: _message(lambda: validate_name(value)),
                        apply=setter(set_name),
                    ),
                    CliConfigSettingDefinition(
                        key="logoUrl", label="Logo",
                        description="An http(s) URL, a root-relative path or a base64 data:image URL; '' for none",
                        type="string", default=defaults.logo, validation=CliConfigValidation(max=200_000),
                        validate=lambda value: None if value == "" else _message(lambda: validate_logo(value)),
                        apply=setter(set_logo),
                    ),
                    CliConfigSettingDefinition(
                        key="themeMode", label="Theme", description="The colour scheme the site opens in",
                        type="enum", default=defaults.theme_mode,
                        validation=CliConfigValidation(options=["system", "light", "dark"]),
                        apply=setter(set_mode),
                    ),
                    CliConfigSettingDefinition(
                        key="footerLinks", label="Footer links",
                        description=(
                            f'At most {MAX_FOOTER_LINKS} links: [{{ "label": "Terms", "url": "https://example.com/terms" }}], '
                            "url http(s) or root-relative"
                        ),
                        type="json", default=copy.deepcopy(defaults.footer_links),
                        validate=footer_links_error,
                        apply=setter(set_links),
                    ),
                ],
            ),
            CliConfigSectionDefinition(
                id="site",
                title="Site",
                description="Set in code (set_path, set_site_dir, add_section)",
                settings=[
                    CliConfigSettingDefinition(
                        key="path", label="Path", description="Where the portal is mounted", type="string",
                        mutable=False, requires_restart=True, default=site.path,
                    ),
                    CliConfigSettingDefinition(
                        key="siteFound", label="Site found",
                        description="Whether the portal site was found (without it nothing is served)",
                        type="boolean", mutable=False, requires_restart=True, default=site.site_found,
                    ),
                    CliConfigSettingDefinition(
                        key="sections", label="Sections",
                        description="The sections published: docs (guides), api (API reference)",
                        type="string[]", mutable=False, requires_restart=True, default=list(site.sections),
                    ),
                ],
            ),
        ],
    )
