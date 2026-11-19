"""Swagger UI for the public document, served from files vendored in this
package (``swagger_ui/``: ``swagger-ui-dist`` 5.35.1, Apache-2.0), so the page
where integrators paste tokens loads nothing from another origin."""

from __future__ import annotations

import html
import json
import os
from typing import Any

SWAGGER_UI_VERSION = "5.35.1"
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swagger_ui")
#: The only files served under ``<base>/swagger/``.
ASSETS: dict[str, str] = {
    "swagger-ui-bundle.js": "text/javascript; charset=utf-8",
    "swagger-ui.css": "text/css; charset=utf-8",
    "favicon-32x32.png": "image/png",
}
PARAMETERS: dict[str, Any] = {
    "dom_id": "#swagger-ui",
    "deepLinking": True,
    "persistAuthorization": False,
    "tryItOutEnabled": True,
}


def is_root_relative(url: Any) -> bool:
    """A same-origin path: starts with one ``/``, nothing that can leave the
    origin or an HTML attribute."""
    return (
        isinstance(url, str)
        and url.startswith("/")
        and not url.startswith("//")
        and not any(c.isspace() or ord(c) < 0x20 or c in "\\\"'<>`" for c in url)
    )


def _script_json(value: Any) -> str:
    """JSON safe inside a ``<script>`` element."""
    return (
        json.dumps(value, indent=2)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )


def swagger_html(*, title: str, openapi_url: str, js_url: str, css_url: str, favicon_url: str) -> str:
    """The page: every URL is HTML-escaped (attributes) or escaped JSON."""
    config = {"url": openapi_url, **PARAMETERS}
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link type="text/css" rel="stylesheet" href="{html.escape(css_url, quote=True)}">
<link rel="icon" type="image/png" href="{html.escape(favicon_url, quote=True)}">
<title>{html.escape(title)}</title>
</head>
<body>
<div id="swagger-ui"></div>
<script src="{html.escape(js_url, quote=True)}"></script>
<script>
const ui = SwaggerUIBundle({_script_json(config)})
</script>
</body>
</html>
"""
