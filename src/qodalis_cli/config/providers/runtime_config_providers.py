"""Built-in writable providers: ``cors``, ``mcp`` and ``filesystem``."""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
)

#: An origin: ``http(s)://host[:port]`` or ``http(s)://host:*`` (any port), no path.
_ORIGIN = re.compile(r"^https?://[^\s/:?#]+(:(\d{1,5}|\*))?\Z", re.IGNORECASE)

#: How CORS headers are decided (``CliServerOptions.cors``).
CORS_MODES = ["origins", "permissive", "custom", "disabled"]


def invalid_origin(value: Any) -> Optional[str]:
    """The first invalid origin of a list, as an error message."""

    def valid(origin: str) -> bool:
        match = _ORIGIN.match(origin.strip().rstrip("/"))
        port = match.group(2) if match else None
        return match is not None and (port is None or port == "*" or int(port) <= 65535)

    bad = next((o for o in value or [] if not valid(o)), None)
    return f"'{bad}' is not an origin (http(s)://host[:port], or :* for any port)" if bad is not None else None


def create_cors_config_provider(origins: list[str], mode: str) -> CliStoredConfigProvider:
    """``cors``: the allowed origins, changeable at runtime.  ``origins`` is
    the server's live list (CORS in ``origins`` mode, WebSocket upgrades, the
    MCP endpoint, the users plugin's CSRF check): changes replace its items
    in place."""
    initial = list(origins)

    def apply(value: Any) -> None:
        items = [o.strip() for o in (value if isinstance(value, list) else initial)]
        origins[:] = items

    return CliStoredConfigProvider(
        id="cors",
        title="CORS",
        description="Origins allowed to call the server from a browser and to open its WebSockets",
        order=30,
        sections=[CliConfigSectionDefinition(id="cors", title="CORS", settings=[
            CliConfigSettingDefinition(
                key="mode",
                label="Mode",
                description=(
                    "origins: CORS headers for the allowed origins (and same-origin); permissive: any origin; "
                    "custom: the host's cors_origins; disabled: none"
                ),
                type="enum",
                mutable=False,
                default=mode,
                validation=CliConfigValidation(options=list(CORS_MODES)),
            ),
            CliConfigSettingDefinition(
                key="allowedOrigins",
                label="Allowed origins",
                description=(
                    "Exact origins (http://host:port; http://host:* for any port). Used for CORS in origins mode, "
                    "WebSocket upgrades, the MCP endpoint and CSRF checks"
                ),
                type="string[]",
                default=initial,
                editor="origins",
                validate=invalid_origin,
                apply=apply,
            ),
        ])],
    )


_TOOL_PATTERN = CliConfigValidation(pattern="^[a-z0-9_*-]{1,64}$")


def create_mcp_config_provider(options: Any, path: str) -> CliStoredConfigProvider:
    """``mcp``: the MCP endpoint's tool lists, instructions and read-only
    mode, changeable at runtime (``options`` is the endpoint's live
    ``CliMcpOptions``).  Read-only when MCP is off."""
    enabled = options is not None
    tools = list(options.tools) if enabled and options.tools else []
    exclude = list(options.exclude) if enabled and options.exclude else []
    instructions = (options.instructions or "") if enabled else ""
    read_only = bool(options.read_only) if enabled else False

    def setter(attr: str, convert: Callable[[Any], Any]) -> Callable[[Any], None]:
        def apply(value: Any) -> None:
            if options is not None:
                setattr(options, attr, convert(value))
        return apply

    def as_list(value: Any) -> Optional[list[str]]:
        return list(value) if isinstance(value, list) and value else None

    return CliStoredConfigProvider(
        id="mcp",
        title="MCP",
        description="The MCP endpoint: which tools AI clients see",
        order=40,
        sections=[CliConfigSectionDefinition(id="mcp", title="MCP endpoint", settings=[
            CliConfigSettingDefinition(
                key="enabled", label="Enabled", description="The MCP endpoint is served (CliServerOptions.mcp)",
                type="boolean", mutable=False, requires_restart=True, default=enabled,
            ),
            CliConfigSettingDefinition(
                key="path", label="Path", description="Where the endpoint is served",
                type="string", mutable=False, default=path,
            ),
            CliConfigSettingDefinition(
                key="tools", label="Allowed tools",
                description="Allow-list of tool names (a trailing * matches a prefix); empty: every tool",
                type="string[]", default=tools, editor="tags", mutable=enabled,
                validation=_TOOL_PATTERN, apply=setter("tools", as_list),
            ),
            CliConfigSettingDefinition(
                key="exclude", label="Excluded tools",
                description="Deny-list of tool names, applied after the allow-list",
                type="string[]", default=exclude, editor="tags", mutable=enabled,
                validation=_TOOL_PATTERN, apply=setter("exclude", as_list),
            ),
            CliConfigSettingDefinition(
                key="instructions", label="Instructions", description="Returned to clients by initialize",
                type="string", default=instructions, editor="textarea", mutable=enabled,
                validation=CliConfigValidation(max=4000),
                apply=setter("instructions", lambda v: v if isinstance(v, str) and v else None),
            ),
            CliConfigSettingDefinition(
                key="readOnly", label="Read-only",
                description="Only tools annotated read-only (readOnlyHint) are listed and callable",
                type="boolean", default=read_only, mutable=enabled,
                apply=setter("read_only", lambda v: v is True),
            ),
        ])],
    )


#: The largest upload limit the ``filesystem`` provider accepts (10 GiB).
MAX_UPLOAD_LIMIT = 10 * 1024 * 1024 * 1024


class FileSystemApiLimits:
    """The filesystem API's live upload limit (read per upload)."""

    def __init__(self, max_upload_bytes: int) -> None:
        self.max_upload_bytes = max_upload_bytes

    def __call__(self) -> int:
        return self.max_upload_bytes


def create_filesystem_config_provider(
    storage: str, limits: FileSystemApiLimits, roots: Optional[list[str]],
) -> CliStoredConfigProvider:
    """``filesystem``: the filesystem API.  The upload limit changes at
    runtime (``limits`` is read per upload); the storage and the allowed
    roots are read-only on purpose: widening what the server may read is a
    deployment decision, not a form field."""
    initial_limit = limits.max_upload_bytes

    def apply(value: Any) -> None:
        limits.max_upload_bytes = value if isinstance(value, int) and not isinstance(value, bool) else initial_limit

    def whole(value: Any) -> Optional[str]:
        return None if isinstance(value, int) or (isinstance(value, float) and value.is_integer()) else (
            "must be a whole number of bytes"
        )

    def apply_number(value: Any) -> None:
        apply(int(value) if isinstance(value, float) else value)

    return CliStoredConfigProvider(
        id="filesystem",
        title="Filesystem",
        description="The filesystem API (/api/qcli/fs)",
        order=50,
        sections=[CliConfigSectionDefinition(id="filesystem", title="Filesystem", settings=[
            CliConfigSettingDefinition(
                key="storage", label="Storage", description="The storage provider",
                type="string", mutable=False, default=storage,
            ),
            CliConfigSettingDefinition(
                key="allowedPaths", label="Allowed roots",
                description="Directories the API may access (OS storage); set in code",
                type="string[]", mutable=False, requires_restart=True, default=list(roots or []),
            ),
            CliConfigSettingDefinition(
                key="maxUploadBytes", label="Upload limit",
                description="Largest file accepted by POST /upload, in bytes",
                type="number", default=initial_limit,
                validation=CliConfigValidation(min=1, max=MAX_UPLOAD_LIMIT),
                validate=whole, apply=apply_number,
            ),
        ])],
    )
