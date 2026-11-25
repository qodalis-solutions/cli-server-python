"""Fluent builder for the portal plugin.

Usage::

    portal = (
        CliPortalBuilder()
        .set_name("RateBell API")
        .add_footer_link("Terms", "https://ratebell.app/terms")
        .add_section(docs_plugin)
        .add_section(public_api_plugin)
        .build()
    )
    result.mount_plugin(portal)

The portal answers only its own paths under the mount (the site's pages,
``portal.json``, ``assets/<file>``, ``favicon.svg``; GET and HEAD), so even at
``/`` it never shadows the host's routes.
"""

from __future__ import annotations

import copy
import html
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.routing import Match

from .options import (
    validate_footer_links,
    validate_logo,
    validate_name,
    validate_path,
    validate_theme,
)
from .portal_command import CliPortalCommandProcessor, PortalModule, PortalStatus
from .portal_config_provider import PortalBranding, PortalSiteFacts, create_portal_config_provider
from .site_resolver import PortalSiteSource, resolve_site

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/qcli/portal"
DEFAULT_NAME = "Documentation"
SITE_NOT_FOUND = "[qcli] portal site not found; the portal is not served"
INVALID_PATH_BASE = "invalid path base"
SECTION_KINDS = ("docs", "api")
BASE_TAG = '<base href="/" />'
_NO_CACHE = {"Cache-Control": "no-cache"}
JSON_TYPE = "application/json; charset=utf-8"
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
_ASSET_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_METHODS = ("GET", "HEAD")


class _PortalRoute(APIRoute):
    """A route that matches GET and HEAD only, and (for files) only when the
    file exists, so anything else falls through to the host's routes and
    the framework's 404."""

    #: ``(path_params) -> bool``: whether the route serves this request.
    serves: Optional[Callable[[dict[str, Any]], bool]] = None

    def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:
        match, child = super().matches(scope)
        if match == Match.NONE:
            return match, child
        if scope.get("method") not in _METHODS:
            return Match.NONE, {}
        serves = type(self).serves
        if serves is not None and not serves(child.get("path_params", {})):
            return Match.NONE, {}
        return match, child


def _route_class(serves: Callable[[dict[str, Any]], bool] | None = None) -> type[_PortalRoute]:
    # A class per condition: include_router re-creates routes from their class.
    return type("PortalRoute", (_PortalRoute,), {"serves": staticmethod(serves) if serves else None})


def _path_base(request: Request) -> str | None:
    """The app's root path, or ``None`` when it could make a URL leave the
    host: ``//`` or ``/\\``, or whitespace / control characters (browsers drop
    them, so ``/<tab>/evil`` becomes ``//evil``)."""
    root = request.scope.get("root_path", "") or ""
    if root.startswith("//") or root.startswith("/\\") or any(c.isspace() or ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in root):
        return None
    return root.rstrip("/")


@dataclass
class CliPortalPlugin:
    """The built portal plugin: mount it with ``CliServerResult.mount_plugin``."""

    #: The mount (``/`` allowed).
    prefix: str
    router: APIRouter
    #: ``portal.json`` without a path base.
    document: dict[str, Any]
    #: The site's directory, ``None`` when no site was found (nothing served).
    site_dir: Optional[str] = None
    #: Host routes: never counted among the CLI's routes.
    cli_routes: bool = False
    #: Served without sign-in (GET and HEAD), also under ``/api/`` on a
    #: protected server; under them the portal answers every path itself.
    #: Empty at ``/`` (never under ``/api/``) and without a site.
    public_paths: list[str] = field(default_factory=list)
    #: Where the site was found (``siteDir``, ``package``, ``checkout``, ``bundled``); ``None`` without one.
    site_source: Optional[PortalSiteSource] = None
    #: The ``portal`` config provider: branding changeable at runtime
    #: (``config set portal.branding.<key>``), the site read-only.
    config_providers: list[Any] = field(default_factory=list)
    #: The ``portal`` terminal command (``portal [status] [--json]``, ``config:read``), registered by ``mount_plugin``.
    module: Any = None
    #: The docs section's page count (the ``portal`` command shows it).
    page_count: Optional[int] = None
    _index: Optional[str] = field(default=None, repr=False)

    def apply_branding(self, branding: PortalBranding) -> None:
        """Puts the branding in ``portal.json`` (the ``portal`` config provider calls it on every change)."""
        self.document["name"] = branding.name
        self.document["logo"] = branding.logo
        self.document["theme"] = {**self.document["theme"], "mode": branding.theme_mode}
        self.document["footerLinks"] = [dict(link) for link in branding.footer_links]

    def status(self) -> PortalStatus:
        """What ``portal --json`` prints: the current ``portal.json`` (docs with its page count) and where the site is."""
        doc = copy.deepcopy(self.portal_json(""))
        if doc.get("docs"):
            doc["docs"] = {**doc["docs"], "pageCount": self.page_count}
        doc["site"] = {
            "path": self.prefix, "found": self.site_dir is not None, "source": self.site_source, "dir": self.site_dir,
        }
        return doc

    def portal_json(self, root: str) -> dict[str, Any]:
        doc = dict(self.document)
        if doc.get("docs"):
            doc["docs"] = {**doc["docs"], "pages": root + doc["docs"]["pages"]}
        if doc.get("api"):
            api = doc["api"]
            doc["api"] = {**api, "openapi": root + api["openapi"], "swagger": root + api["swagger"]}
        return doc

    def index_html(self, root: str) -> str:
        mount = "" if self.prefix == "/" else self.prefix
        href = html.escape(f"{root}{mount}/", quote=True)
        return (self._index or "").replace(BASE_TAG, f'<base href="{href}" />', 1)


class CliPortalBuilder:
    """Fluent builder for the portal plugin.  Everything is checked by
    :meth:`build` (``ValueError``)."""

    def __init__(self) -> None:
        self._path: Any = DEFAULT_PATH
        self._name: Any = DEFAULT_NAME
        self._logo: Any = None
        self._theme: Any = None
        self._links: list[tuple[Any, Any]] = []
        self._sections: list[Any] = []
        self._site_dir: str | None = None

    def set_path(self, path: str) -> CliPortalBuilder:
        """The mount (default ``/qcli/portal``; ``/`` allowed)."""
        self._path = path
        return self

    def set_name(self, name: str) -> CliPortalBuilder:
        """The portal's name (default ``Documentation``): 1-80 characters."""
        self._name = name
        return self

    def set_logo_url(self, url: str | None) -> CliPortalBuilder:
        """An http(s) URL, a root-relative path or a base64 ``data:image`` URL."""
        self._logo = url
        return self

    def set_theme(self, theme: dict[str, Any] | None) -> CliPortalBuilder:
        """``{"mode": "dark" | "light" | "system", "dark": {...}, "light":
        {...}}`` with the dashboard's colour tokens (camelCase or
        snake_case); the mode defaults to ``system``."""
        self._theme = theme
        return self

    def add_footer_link(self, label: str, url: str) -> CliPortalBuilder:
        """A footer link (at most 20): an http(s) URL or a root-relative path."""
        self._links.append((label, url))
        return self

    def add_section(self, plugin: Any) -> CliPortalBuilder:
        """Show a plugin's section: the docs plugin (guides) or the public-api
        plugin (API reference); a ``portal_section`` dict works too."""
        section = getattr(plugin, "portal_section", plugin)
        # The docs plugin's page count, shown by the ``portal`` command
        pages = getattr(plugin, "pages", None)
        self._sections.append((section, len(pages) if isinstance(pages, (list, tuple)) else None))
        return self

    def set_site_dir(self, path: str | None) -> CliPortalBuilder:
        """Serve the site from this directory only (no other location is
        tried); by default it is found as described in the README."""
        self._site_dir = path
        return self

    def build(self) -> CliPortalPlugin:
        path = validate_path(self._path)
        document: dict[str, Any] = {
            "name": validate_name(self._name),
            "logo": validate_logo(self._logo),
            "theme": validate_theme(self._theme),
            "footerLinks": validate_footer_links(self._links),
            "docs": None,
            "api": None,
        }
        seen: set[str] = set()
        page_count: Optional[int] = None
        for section, pages in self._sections:
            kind = section.get("kind") if isinstance(section, dict) else None
            if kind not in SECTION_KINDS:
                raise ValueError(f"invalid portal section: {kind}")
            if kind in seen:
                raise ValueError(f"portal section already registered: {kind}")
            seen.add(kind)
            base = section["base_path"]
            if kind == "docs":
                document["docs"] = {"title": section["title"], "pages": f"{base}/pages"}
                page_count = pages
            else:
                document["api"] = {
                    "title": section["title"], "openapi": f"{base}/openapi.json", "swagger": f"{base}/swagger",
                }

        site = resolve_site(self._site_dir)
        site_dir = site[0] if site else None
        plugin = CliPortalPlugin(
            prefix=path, router=APIRouter(), document=document, site_dir=site_dir,
            site_source=site[1] if site else None, page_count=page_count,
        )
        # The builder's branding is the default; the ``portal`` config provider's stored values override it (live)
        defaults = PortalBranding(
            name=document["name"], logo=document["logo"], theme_mode=document["theme"]["mode"],
            footer_links=[dict(link) for link in document["footerLinks"]],
        )
        plugin.config_providers = [
            create_portal_config_provider(
                defaults,
                plugin.apply_branding,
                PortalSiteFacts(
                    path=path, site_found=site_dir is not None,
                    sections=[s.get("kind") for s, _ in self._sections],
                ),
            ),
        ]
        plugin.module = PortalModule(CliPortalCommandProcessor(plugin.status), _package_version())
        if site_dir is None:
            # Nothing is served, portal.json included.
            logger.warning(SITE_NOT_FOUND)
            return plugin
        with open(os.path.join(site_dir, "index.html"), encoding="utf-8") as handle:
            plugin._index = handle.read()
        plugin.router = _router(plugin, site_dir)
        if path != "/":
            plugin.public_paths = [
                path, f"{path}/", f"{path}/portal.json", f"{path}/favicon.svg", f"{path}/assets/*",
                f"{path}/guides/*", f"{path}/reference", f"{path}/reference/*",
            ]
        return plugin


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("qodalis-cli-server-plugin-portal")
    except Exception:  # pragma: no cover - not installed as a package
        return "1.0.0"


def _router(plugin: CliPortalPlugin, site_dir: str) -> APIRouter:
    router = APIRouter()
    assets_dir = os.path.join(site_dir, "assets")
    favicon = os.path.join(site_dir, "favicon.svg")

    def asset_exists(params: dict[str, Any]) -> bool:
        name = params.get("file")
        return (
            isinstance(name, str)
            and bool(_ASSET_NAME.match(name))
            and os.path.isfile(os.path.join(assets_dir, name))
        )

    async def index(request: Request) -> Response:
        root = _path_base(request)
        if root is None:
            return JSONResponse({"error": INVALID_PATH_BASE}, status_code=400, media_type=JSON_TYPE)
        return HTMLResponse(plugin.index_html(root), headers=_NO_CACHE)

    async def portal_json(request: Request) -> Response:
        root = _path_base(request)
        if root is None:
            return JSONResponse({"error": INVALID_PATH_BASE}, status_code=400, media_type=JSON_TYPE)
        return JSONResponse(plugin.portal_json(root), headers=_NO_CACHE, media_type=JSON_TYPE)

    # At "/" a missing file falls through to the host's routes.  Elsewhere the
    # portal answers every path under its public prefixes itself, so a path
    # served without sign-in never reaches a host route.
    owns_prefixes = plugin.prefix != "/"

    async def not_found() -> Response:
        return JSONResponse({"error": "not found"}, status_code=404, media_type=JSON_TYPE)

    async def asset(file: str) -> Response:
        if not asset_exists({"file": file}):
            return await not_found()
        return FileResponse(os.path.join(assets_dir, file), headers=_IMMUTABLE)

    async def favicon_svg() -> Response:
        if not os.path.isfile(favicon):
            return await not_found()
        return FileResponse(favicon, media_type="image/svg+xml")

    plain = _route_class()

    def add(path: str, endpoint: Any, route_class: type[_PortalRoute] = plain) -> None:
        router.add_api_route(
            path, endpoint, methods=list(_METHODS), include_in_schema=False, route_class_override=route_class,
        )

    # The mount itself ("" under the prefix); at "/" the "/" route covers it.
    if plugin.prefix != "/":
        add("", index)
    for page in ("/", "/guides/{slug}", "/reference", "/reference/{tag}"):
        add(page, index)
    add("/portal.json", portal_json)
    if owns_prefixes:
        add("/assets/{file}", asset)
        add("/favicon.svg", favicon_svg)
        for under in ("/assets", "/guides", "/reference"):
            add(under + "/{rest:path}", not_found)
    else:
        add("/assets/{file}", asset, _route_class(asset_exists))
        add("/favicon.svg", favicon_svg, _route_class(lambda _: os.path.isfile(favicon)))
    return router
