"""The public OpenAPI document (contract C6).

The host hands over the document (``set_openapi``): a ``dict``, a
zero-argument callable returning one, a ``FastAPI`` app or an ``APIRouter``
(read through ``fastapi.openapi.utils.get_openapi``).  For apps and routers
each operation whose guard is tagged (``require_permission``,
``require_host_token``, the public-api guard) gets ``x-required-permission``
and ``x-host-token-only``.  Every document gets the ``qcliToken`` bearer
scheme, a top-level ``security`` requirement and ``servers``, unless it has
its own: the host's values always win.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

SCHEME_NAME = "qcliToken"
SECURITY_SCHEME: dict[str, str] = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "qcli_pat",
    "description": "API token (qcli_pat_…), sent as Authorization: Bearer <token>",
}
DEFAULT_TITLE = "API"
DEFAULT_VERSION = "1.0.0"
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def check_source(source: Any) -> Any:
    if source is None or isinstance(source, (dict, FastAPI, APIRouter)) or callable(source):
        return source
    raise TypeError("public-api: the OpenAPI source must be a dict, a callable, a FastAPI app or an APIRouter")


def _guard_tags(dependant: Any) -> tuple[str | None, bool]:
    """The permission and host-token-only flag of the tagged guards among a
    route's dependencies (searched recursively)."""
    permission: str | None = None
    host_only = False
    stack = list(getattr(dependant, "dependencies", None) or [])
    while stack:
        dep = stack.pop(0)
        call = getattr(dep, "call", None)
        if call is not None and hasattr(call, "qcli_host_token_only"):
            permission = getattr(call, "qcli_permission", None) or permission
            host_only = host_only or bool(getattr(call, "qcli_host_token_only", False))
        stack.extend(getattr(dep, "dependencies", None) or [])
    return permission, host_only


def _stamp_guards(document: dict[str, Any], routes: list[Any]) -> None:
    paths = document.get("paths") or {}
    for route in routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        permission, host_only = _guard_tags(route.dependant)
        if permission is None and not host_only:
            continue
        item = paths.get(route.path_format)
        if not isinstance(item, dict):
            continue
        for method in route.methods or ():
            operation = item.get(method.lower())
            if not isinstance(operation, dict):
                continue
            if permission is not None and "x-required-permission" not in operation:
                operation["x-required-permission"] = permission
            if host_only and "x-host-token-only" not in operation:
                operation["x-host-token-only"] = True


class PublicOpenApiDocument:
    """Builds the document per request from a cached base document."""

    def __init__(
        self,
        source: Any,
        title: str | None = None,
        version: str | None = None,
        server_url: str | None = None,
    ) -> None:
        self.source = check_source(source)
        self.title = title or DEFAULT_TITLE
        self.version = version or DEFAULT_VERSION
        self.server_url = server_url.rstrip("/") if server_url else None
        self._base: dict[str, Any] | None = None

    def _from_routes(self, routes: list[Any]) -> dict[str, Any]:
        document = get_openapi(title=self.title, version=self.version, routes=routes)
        _stamp_guards(document, routes)
        return document

    def _source_document(self) -> dict[str, Any]:
        source = self.source
        if isinstance(source, (FastAPI, APIRouter)):
            # Routes are read once, on the first request (after the host
            # registered them).
            if self._base is None:
                self._base = self._from_routes(list(source.routes))
            return self._base
        if source is None:
            return {"openapi": "3.1.0", "info": {"title": self.title, "version": self.version}, "paths": {}}
        if isinstance(source, dict):
            return source
        document = source()
        if not isinstance(document, dict):
            raise TypeError("public-api: the OpenAPI callable must return a dict")
        return document

    def build(self, request_base_url: str) -> dict[str, Any]:
        """The document for one request: a deep copy of the base document
        with the C6 additions (``servers`` from ``request_base_url`` unless
        a server URL is set)."""
        document = copy.deepcopy(self._source_document())
        components = document.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        if SCHEME_NAME not in schemes:
            schemes[SCHEME_NAME] = dict(SECURITY_SCHEME)
        if "security" not in document:
            document["security"] = [{SCHEME_NAME: []}]
        if "servers" not in document:
            document["servers"] = [{"url": self.server_url or request_base_url.rstrip("/")}]
        return document

    def title_of(self, document: dict[str, Any]) -> str:
        info = document.get("info")
        title = info.get("title") if isinstance(info, dict) else None
        return title if isinstance(title, str) and title else self.title

