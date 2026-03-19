"""SPA-aware static file serving.

Starlette's ``StaticFiles(html=True)`` only maps ``/`` to ``index.html``.
For a single-page application we need *all* non-file routes to fall back to
``index.html`` so that the client-side router can handle them.
"""

from __future__ import annotations

import os

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    """StaticFiles subclass that falls back to ``index.html`` for missing paths.

    This enables client-side routing: any request that doesn't match a real
    file (e.g. ``/login``, ``/dashboard/settings``) is served the SPA shell
    so the JS router can take over.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # File not found — serve index.html for SPA client-side routing
            index = os.path.join(self.directory, "index.html")  # type: ignore[arg-type]
            if os.path.isfile(index):
                return FileResponse(index, media_type="text/html")
            raise
