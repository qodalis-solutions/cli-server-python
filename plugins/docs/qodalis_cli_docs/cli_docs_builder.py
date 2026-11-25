"""Fluent builder for the docs plugin.

Usage::

    docs_plugin = (
        CliDocsBuilder()
        .set_name("RateBell API")
        .set_variable("support", "help@ratebell.app")
        .add_folder("docs")
        .build()
    )
    result.mount_plugin(docs_plugin)

``GET /qcli/docs/pages`` lists the pages, ``GET /qcli/docs/pages/<slug>``
returns one with its markdown; ``{{baseUrl}}``, ``{{name}}`` and the host's
variables are replaced per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .docs_command import CliDocsCommandProcessor, DocsModule
from .pages import (
    DocsPage,
    apply_variables,
    parse_page,
    read_folder,
    sort_pages,
    validate_slug,
    validate_variable,
)

DEFAULT_BASE_PATH = "/qcli/docs"
DEFAULT_TITLE = "Guides"
DEFAULT_NAME = "Documentation"
PAGE_NOT_FOUND = "page not found"
_NO_CACHE = {"Cache-Control": "no-cache"}


class _Json(JSONResponse):
    media_type = "application/json; charset=utf-8"



def validate_base_path(path: Any) -> str:
    """Starts with ``/``, no trailing ``/``, not ``/`` itself."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or path.endswith("/")
        or "//" in path
        or any(c.isspace() or ord(c) < 0x20 or c in "?#\\" for c in path)
    ):
        raise ValueError(f"invalid docs base path: {path}")
    return path


@dataclass
class CliDocsPlugin:
    """The built docs plugin: mount it with ``CliServerResult.mount_plugin``."""

    prefix: str
    router: APIRouter
    #: The pages in page order, variables not replaced.
    pages: list[DocsPage]
    #: What the portal plugin shows (``CliPortalBuilder().add_section(...)``).
    portal_section: dict[str, str]
    name: str = DEFAULT_NAME
    base_url: str | None = None
    variables: dict[str, str] = field(default_factory=dict)
    #: Host routes: never counted among the CLI's routes.
    cli_routes: bool = False
    #: Served without sign-in, also under ``/api/`` on a protected server
    #: (the router answers everything under ``pages/`` itself).
    public_paths: list[str] = field(default_factory=list)
    #: The ``docs`` terminal command (``docs list``, ``docs read <slug>``), registered by ``mount_plugin``.
    module: Any = None

    def variables_for(self, request: Request) -> dict[str, str]:
        base_url = self.base_url or (str(request.base_url).rstrip("/"))
        return {**self.variables, "baseUrl": base_url, "name": self.name}


class CliDocsBuilder:
    """Fluent builder for the docs plugin.  Everything is checked by
    :meth:`build` (``ValueError``)."""

    def __init__(self) -> None:
        self._base_path: Any = DEFAULT_BASE_PATH
        self._title = DEFAULT_TITLE
        self._name = DEFAULT_NAME
        self._base_url: str | None = None
        self._variables: dict[Any, str] = {}
        #: Folders (``str``) and code pages (``(slug, markdown)``), in order.
        self._sources: list[Union[str, tuple[Any, str]]] = []

    def set_base_path(self, path: str) -> CliDocsBuilder:
        """Where the pages are served (default ``/qcli/docs``)."""
        self._base_path = path
        return self

    def set_title(self, title: str) -> CliDocsBuilder:
        """The portal's label of the guides (default ``Guides``)."""
        self._title = title
        return self

    def set_name(self, name: str) -> CliDocsBuilder:
        """``{{name}}`` (default ``Documentation``)."""
        self._name = name
        return self

    def set_base_url(self, url: str | None) -> CliDocsBuilder:
        """``{{baseUrl}}`` (default: the request's ``scheme://host`` and
        root path); a trailing ``/`` is dropped."""
        self._base_url = url
        return self

    def set_variable(self, key: str, value: str) -> CliDocsBuilder:
        """A host variable, ``{{ key }}`` in titles and markdown."""
        self._variables[key] = value
        return self

    def add_folder(self, path: str) -> CliDocsBuilder:
        """Every ``*.md`` file directly in ``path`` (relative to the working
        directory), read once by :meth:`build`."""
        self._sources.append(path)
        return self

    def add_page(self, slug: str, markdown: str) -> CliDocsBuilder:
        """A page given in code (same front matter rules as files)."""
        self._sources.append((slug, markdown))
        return self

    def build(self) -> CliDocsPlugin:
        base_path = validate_base_path(self._base_path)
        variables = {validate_variable(k): str(v) for k, v in self._variables.items()}
        pages: list[DocsPage] = []
        seen: set[str] = set()
        for source in self._sources:
            if isinstance(source, str):
                found = read_folder(source)
            else:
                slug, markdown = source
                found = [parse_page(validate_slug(slug), markdown)]
            for page in found:
                if page.slug in seen:
                    raise ValueError(f"duplicate docs page: {page.slug}")
                seen.add(page.slug)
                pages.append(page)

        plugin = CliDocsPlugin(
            prefix=base_path,
            router=APIRouter(),
            pages=sort_pages(pages),
            portal_section={"kind": "docs", "title": self._title, "base_path": base_path},
            name=self._name,
            base_url=self._base_url.rstrip("/") if self._base_url else None,
            variables=variables,
        )
        plugin.router = _router(plugin)
        plugin.public_paths = [f"{base_path}/pages", f"{base_path}/pages/*"]
        # No request in a command: {{baseUrl}} is the configured base URL, else empty (links relative to the server root)
        plugin.module = DocsModule(
            CliDocsCommandProcessor(plugin.pages, lambda: {**plugin.variables, "baseUrl": plugin.base_url or "", "name": plugin.name}),
            _package_version(),
        )
        return plugin


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("qodalis-cli-server-plugin-docs")
    except Exception:  # pragma: no cover - not installed as a package
        return "1.0.0"


INVALID_PATH_BASE = "invalid path base"


def _path_base_invalid(request: Request) -> bool:
    """A root path that would make ``{{baseUrl}}`` leave the host: ``//``,
    ``/\\``, or whitespace / control characters (browsers drop them)."""
    root = request.scope.get("root_path", "") or ""
    return root.startswith("//") or root.startswith("/\\") or any(c.isspace() or ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in root)


def _router(plugin: CliDocsPlugin) -> APIRouter:
    router = APIRouter()
    by_slug = {page.slug: page for page in plugin.pages}

    async def list_pages(request: Request) -> _Json:
        if _path_base_invalid(request):
            return _Json({"error": INVALID_PATH_BASE}, status_code=400)
        # Page order is the titles' as written, before variables are replaced.
        variables = plugin.variables_for(request)
        return _Json([apply_variables(p, variables).summary() for p in plugin.pages], headers=_NO_CACHE)

    async def get_page(slug: str, request: Request) -> _Json:
        if _path_base_invalid(request):
            return _Json({"error": INVALID_PATH_BASE}, status_code=400)
        page = by_slug.get(slug)
        if page is None:
            return _Json({"error": PAGE_NOT_FOUND}, status_code=404)
        return _Json(apply_variables(page, plugin.variables_for(request)).to_dict(), headers=_NO_CACHE)

    router.add_api_route("/pages", list_pages, methods=["GET", "HEAD"], include_in_schema=False)
    # ``path`` so an encoded ``/`` (``..%2Fsecret``) still gets the plugin's 404.
    router.add_api_route("/pages/{slug:path}", get_page, methods=["GET", "HEAD"], include_in_schema=False)
    return router
