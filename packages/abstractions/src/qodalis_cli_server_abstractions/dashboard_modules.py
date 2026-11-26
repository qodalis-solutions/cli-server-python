"""Dashboard modules: pages plugins add to the admin dashboard, built in the dashboard's repo
(``cli-server-dashboard/modules/<dir>``, npm ``@qodalis/cli-dashboard-module-<dir>``) and embedded in the plugin
that serves their API.

A plugin copies the module's build into its package (``scripts/copy_dashboard.py``), finds it with
:func:`find_dashboard_module` and serves it with :func:`dashboard_module_page`: the files at a prefix outside the
API (the dashboard's ``import()`` sends no token), and the dashboard extension pointing at them. The server mounts
the pages a plugin lists in ``dashboard_pages``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Where the five plugin pages are served (``/qcli/modules/<id>``), clear of what plugins serve at ``/qcli/<name>``.
DASHBOARD_MODULES_PREFIX = "/qcli/modules"


@dataclass(frozen=True)
class DashboardModulePage:
    """A module's files (an ASGI app serving them without caching) at ``prefix``, and its dashboard extension
    (the JSON shape of ``GET /api/v1/qcli/dashboard/extensions``)."""

    prefix: str
    app: Any
    extension: Mapping[str, Any]


def find_dashboard_module(package_dir: str, directory: Optional[str] = None) -> Optional[str]:
    """The module's build: ``directory`` when given, else the copy in ``<package_dir>/dashboard``; ``None`` when
    it holds no ``extension.js``."""
    candidate = directory or os.path.join(package_dir, "dashboard")
    return candidate if os.path.isfile(os.path.join(candidate, "extension.js")) else None


#: ``/*! <package> <version> */``, the first line of a module's ``extension.js`` (the dashboard repo's module builds).
_MODULE_BANNER = re.compile(r"^/\*!\s+((?:@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*)\s+(\S{1,50})\s+\*/")


def dashboard_module_identity(directory: str) -> Optional[dict[str, str]]:
    """The npm package and version of a module's build (``{"package", "version"}``): the banner of its
    ``extension.js``, else the ``package.json`` of the npm package whose ``dist`` it is. ``None`` when neither says."""
    try:
        with open(os.path.join(directory, "extension.js"), encoding="utf-8", errors="replace") as f:
            banner = _MODULE_BANNER.match(f.read(300))
        if banner:
            return {"package": banner.group(1), "version": banner.group(2)}
    except OSError:
        pass
    for candidate in (os.path.join(directory, "package.json"), os.path.join(directory, "..", "package.json")):
        try:
            with open(candidate, encoding="utf-8") as f:
                pkg = json.load(f)
        except (OSError, ValueError):
            continue
        name, version = (pkg.get("name"), pkg.get("version")) if isinstance(pkg, dict) else (None, None)
        if isinstance(name, str) and "cli-dashboard-" in name and isinstance(version, str):
            return {"package": name, "version": version}
    return None


def _content_hash(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def dashboard_module_page(directory: str, prefix: str, extension: Mapping[str, Any]) -> DashboardModulePage:
    """Serves the build in ``directory`` at ``prefix`` and completes ``extension`` (``id``, ``title``, ``titles``,
    ``icon``, ``group``, ``order``, ``permission``, ``widgets``, ``search``) with ``entry`` and ``styles``, whose
    URLs carry the files' content hash (``?v=``), so a new build is fetched at once. ``package`` and ``version`` come
    from the build (:func:`dashboard_module_identity`) unless ``extension`` gives them."""
    from starlette.staticfiles import StaticFiles

    class _NoCacheStaticFiles(StaticFiles):
        def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    prefix = "/" + prefix.strip("/")
    out: dict[str, Any] = {k: v for k, v in dict(extension).items() if v is not None and k not in ("entry", "styles")}
    if not out.get("widgets"):
        out.pop("widgets", None)
    identity = dashboard_module_identity(directory)
    if identity:
        out = {**identity, **out}
    out["entry"] = f"{prefix}/extension.js?v={_content_hash(os.path.join(directory, 'extension.js'))}"
    css = _content_hash(os.path.join(directory, "extension.css"))
    if css:
        out["styles"] = [f"{prefix}/extension.css?v={css}"]
    return DashboardModulePage(prefix=prefix, app=_NoCacheStaticFiles(directory=directory), extension=out)


def module_page_of(
    package_dir: str,
    directory: Optional[str] | bool,
    prefix: str,
    extension: Mapping[str, Any],
) -> Optional[DashboardModulePage]:
    """The page of a plugin: ``directory`` ``False`` leaves it out, a path serves that build, ``None`` the copy in
    the package; ``None`` when there is no build."""
    if directory is False:
        return None
    found = find_dashboard_module(package_dir, directory if isinstance(directory, str) else None)
    if not found:
        logging.getLogger(__name__).warning(
            "[qcli] the dashboard's %s page was not found; the dashboard will not show it", extension.get("id"),
        )
        return None
    return dashboard_module_page(found, prefix, extension)
