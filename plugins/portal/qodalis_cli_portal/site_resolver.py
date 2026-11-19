"""Finds the portal site's built files (``index.html``, ``assets/``)."""

from __future__ import annotations

import os

PACKAGE = os.path.join("node_modules", "@qodalis", "cli-server-portal", "dist")


def _site(path: str) -> str | None:
    return os.path.abspath(path) if os.path.isfile(os.path.join(path, "index.html")) else None


def resolve_site_dir(explicit_path: str | None = None) -> str | None:
    """The site directory, or ``None``.

    An explicit path is used alone (``None`` when it has no ``index.html``).
    Otherwise, in order: the npm package
    (``node_modules/@qodalis/cli-server-portal/dist``, searched upwards from
    the working directory), the sibling repository's build in a development
    checkout (``cli-server-dashboard/portal/dist`` next to this repository)
    and the copy embedded in the package (``site/``, see
    ``scripts/copy_portal.py``).
    """
    if explicit_path is not None:
        return _site(explicit_path)

    current = os.getcwd()
    while current != os.path.dirname(current):
        found = _site(os.path.join(current, PACKAGE))
        if found:
            return found
        current = os.path.dirname(current)

    this_dir = os.path.dirname(os.path.abspath(__file__))
    # plugins/portal/qodalis_cli_portal in a checkout of cli-server-python.
    checkout = os.path.abspath(os.path.join(this_dir, "..", "..", ".."))
    if os.path.isfile(os.path.join(checkout, "plugins", "portal", "pyproject.toml")):
        found = _site(os.path.join(checkout, "..", "cli-server-dashboard", "portal", "dist"))
        if found:
            return found

    return _site(os.path.join(this_dir, "site"))
