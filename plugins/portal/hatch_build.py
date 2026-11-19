"""Refuses to build a wheel or sdist of the portal plugin without the portal
site copied into the package (``python scripts/copy_portal.py`` first).
Editable installs (development) are not checked."""

from __future__ import annotations

import os

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class PortalSiteHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return
        index = os.path.join(self.root, "qodalis_cli_portal", "site", "index.html")
        if not os.path.isfile(index):
            raise RuntimeError(
                "portal site not found: run `python scripts/copy_portal.py` (after `npm install`) "
                "before building qodalis-cli-server-plugin-portal"
            )
