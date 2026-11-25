"""Finds the portal site's built files (``index.html``, ``assets/``)."""

from __future__ import annotations

import os
from typing import Literal

PACKAGE = os.path.join("node_modules", "@qodalis", "cli-server-portal", "dist")


def _site(path: str) -> str | None:
    return os.path.abspath(path) if os.path.isfile(os.path.join(path, "index.html")) else None


#: Where the portal site was found: ``siteDir`` (set with ``set_site_dir``), the installed
#: ``package``, the sibling ``checkout`` or the ``bundled`` copy.
PortalSiteSource = Literal["siteDir", "package", "checkout", "bundled"]

#: Readable names of the sources (the ``portal`` command shows them).
PORTAL_SITE_SOURCES: dict[str, str] = {
    "siteDir": "set_site_dir",
    "package": "@qodalis/cli-server-portal",
    "checkout": "cli-server-dashboard/portal/dist",
    "bundled": "bundled with the plugin",
}


def resolve_site_dir(explicit_path: str | None = None) -> str | None:
    """The site directory (:func:`resolve_site`), or ``None``."""
    site = resolve_site(explicit_path)
    return site[0] if site else None


def resolve_site(explicit_path: str | None = None) -> tuple[str, PortalSiteSource] | None:
    """The site directory and where it was found, or ``None``.

    An explicit path is used alone (``None`` when it has no ``index.html``).
    Otherwise, in order: the npm package
    (``node_modules/@qodalis/cli-server-portal/dist``, searched upwards from
    the working directory), the sibling repository's build in a development
    checkout (``cli-server-dashboard/portal/dist`` next to this repository)
    and the copy embedded in the package (``site/``, see
    ``scripts/copy_portal.py``).
    """
    if explicit_path is not None:
        found = _site(explicit_path)
        return (found, "siteDir") if found else None

    current = os.getcwd()
    while current != os.path.dirname(current):
        found = _site(os.path.join(current, PACKAGE))
        if found:
            return found, "package"
        current = os.path.dirname(current)

    this_dir = os.path.dirname(os.path.abspath(__file__))
    # plugins/portal/qodalis_cli_portal in a checkout of cli-server-python.
    checkout = os.path.abspath(os.path.join(this_dir, "..", "..", ".."))
    if os.path.isfile(os.path.join(checkout, "plugins", "portal", "pyproject.toml")):
        found = _site(os.path.join(checkout, "..", "cli-server-dashboard", "portal", "dist"))
        if found:
            return found, "checkout"

    found = _site(os.path.join(this_dir, "site"))
    return (found, "bundled") if found else None
