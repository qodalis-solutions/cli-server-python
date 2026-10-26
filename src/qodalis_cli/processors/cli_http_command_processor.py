from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from qodalis_cli_server_abstractions import ICliStreamCommandProcessor

from ..abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
    get_positionals,
)


def _url(command: CliProcessCommand) -> str | None:
    """The URL: the first positional argument, unquoted."""
    positionals = get_positionals(command)
    return positionals[0] if positionals else None


#: Maximum number of redirects followed by one request.
MAX_REDIRECTS = 5

#: Address ranges the ``http`` command refuses unless ``allow_private_networks``
#: is set: loopback, private, link-local (cloud metadata), CGNAT, unspecified.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",  # loopback
        "10.0.0.0/8",  # private
        "172.16.0.0/12",  # private
        "192.168.0.0/16",  # private
        "169.254.0.0/16",  # link-local (169.254.169.254 = cloud metadata)
        "100.64.0.0/10",  # carrier-grade NAT
        "0.0.0.0/8",  # unspecified / "this network"
        "::1/128",  # loopback
        "::/128",  # unspecified
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
    )
]


class HttpRequestBlockedError(Exception):
    """A request (or a redirect hop) targets a private or local address."""


def _blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1
    return any(ip.version == net.version and ip in net for net in _BLOCKED_NETWORKS)


def _resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to (an IP literal resolves to itself)."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise urllib.error.URLError(e) from e
    return [info[4][0] for info in infos]


def check_url_allowed(url: str, allow_private_networks: bool = False) -> None:
    """Raise :class:`HttpRequestBlockedError` unless ``url`` is http(s) and
    none of its host's addresses is private or local.

    Note: the host is resolved again when connecting, so a DNS server that
    answers differently the second time (rebinding) is not covered.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise HttpRequestBlockedError(
            f"Unsupported URL scheme '{scheme or '(none)'}': only http and https are allowed."
        )
    if allow_private_networks:
        return
    host = parts.hostname or ""
    if not host:
        raise HttpRequestBlockedError(f"Invalid URL: {url}")
    for address in _resolve(host):
        # Drop an IPv6 zone id (fe80::1%en0) before parsing.
        if _blocked_address(ipaddress.ip_address(address.split("%", 1)[0])):
            raise HttpRequestBlockedError(
                f"Blocked: {host} resolves to a private or local address ({address}). "
                "Set allow_private_networks to allow it."
            )


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Checks every redirect hop and follows at most :data:`MAX_REDIRECTS`."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_private_networks: bool) -> None:
        super().__init__()
        self._allow_private_networks = allow_private_networks

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url_allowed(newurl, self._allow_private_networks)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, method: str, body: str | None, allow_private_networks: bool):
    """``urlopen`` with the target and every redirect hop checked."""
    check_url_allowed(url, allow_private_networks)
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(_GuardedRedirectHandler(allow_private_networks))
    return opener.open(req, timeout=30)


class _HttpGetProcessor(CliCommandProcessor, ICliStreamCommandProcessor):
    """Sub-processor that performs HTTP GET requests."""

    def __init__(self, allow_private_networks: bool = False) -> None:
        self._allow_private_networks = allow_private_networks

    @property
    def command(self) -> str:
        return "get"

    @property
    def value_required(self) -> bool | None:
        # Takes an optional positional value (exposed as the MCP tool's ``value``).
        return False

    @property
    def description(self) -> str:
        return "Performs an HTTP GET request"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="headers",
                description="Show response headers",
                type="boolean",
            ),
        ]

    async def handle_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> str:
        url = _url(command)
        if not url:
            return "Usage: http get <url>"
        return _do_request(
            url,
            method="GET",
            show_headers="headers" in command.args,
            allow_private_networks=self._allow_private_networks,
        )

    async def handle_stream_async(
        self,
        command: CliProcessCommand,
        emit,
        cancellation_event: asyncio.Event | None = None,
    ) -> int:
        url = _url(command)
        if not url:
            emit({"type": "text", "value": "Usage: http get <url>"})
            return 1
        return await _do_stream_request(
            url, "GET", None, "headers" in command.args, emit, cancellation_event,
            allow_private_networks=self._allow_private_networks,
        )


class _HttpPostProcessor(CliCommandProcessor, ICliStreamCommandProcessor):
    """Sub-processor that performs HTTP POST requests."""

    def __init__(self, allow_private_networks: bool = False) -> None:
        self._allow_private_networks = allow_private_networks

    @property
    def command(self) -> str:
        return "post"

    @property
    def value_required(self) -> bool | None:
        # Takes an optional positional value (exposed as the MCP tool's ``value``).
        return False

    @property
    def description(self) -> str:
        return "Performs an HTTP POST request"

    @property
    def parameters(self) -> list[ICliCommandParameterDescriptor]:
        return [
            CliCommandParameterDescriptor(
                name="body",
                description="Request body (JSON string)",
                aliases=["-b"],
                type="string",
            ),
            CliCommandParameterDescriptor(
                name="headers",
                description="Show response headers",
                type="boolean",
            ),
        ]

    async def handle_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> str:
        url = _url(command)
        if not url:
            return "Usage: http post <url> --body '{\"key\":\"value\"}'"
        body = command.args.get("body")
        return _do_request(
            url,
            method="POST",
            body=body,
            show_headers="headers" in command.args,
            allow_private_networks=self._allow_private_networks,
        )

    async def handle_stream_async(
        self,
        command: CliProcessCommand,
        emit,
        cancellation_event: asyncio.Event | None = None,
    ) -> int:
        url = _url(command)
        if not url:
            emit({"type": "text", "value": "Usage: http post <url> --body '{\"key\":\"value\"}'"})
            return 1
        body = command.args.get("body")
        return await _do_stream_request(
            url, "POST", body, "headers" in command.args, emit, cancellation_event,
            allow_private_networks=self._allow_private_networks,
        )


async def _do_stream_request(
    url: str,
    method: str,
    body: str | None,
    show_headers: bool,
    emit,
    cancellation_event: asyncio.Event | None = None,
    allow_private_networks: bool = False,
) -> int:
    try:
        if cancellation_event and cancellation_event.is_set():
            return 1
        emit({"type": "text", "value": f"Fetching {method} {url}...", "style": "info"})

        with _open(url, method, body, allow_private_networks) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "unknown")
            resp_body = resp.read().decode("utf-8", errors="replace")

            emit({"type": "text", "value": f"Status: {status}"})
            emit({"type": "text", "value": f"Content-Type: {content_type}"})

            if show_headers:
                emit({"type": "text", "value": "Headers:"})
                for key, value in resp.headers.items():
                    emit({"type": "text", "value": f"  {key}: {value}"})

            if "json" in content_type:
                try:
                    parsed = json.loads(resp_body)
                    resp_body = json.dumps(parsed, indent=2)
                except json.JSONDecodeError:
                    pass

            emit({"type": "text", "value": resp_body[:5000]})
            return 0

    except HttpRequestBlockedError as e:
        emit({"type": "text", "value": str(e), "style": "error"})
        return 1
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:2000] if e.fp else ""
        emit({"type": "text", "value": f"HTTP Error: {e.code} {e.reason}\n{body_text}", "style": "error"})
        return 1
    except urllib.error.URLError as e:
        emit({"type": "text", "value": f"Connection error: {e.reason}", "style": "error"})
        return 1
    except Exception as e:
        emit({"type": "text", "value": f"Error: {e}", "style": "error"})
        return 1


def _do_request(
    url: str,
    method: str = "GET",
    body: str | None = None,
    show_headers: bool = False,
    allow_private_networks: bool = False,
) -> str:
    """Perform an HTTP request and return a formatted text summary.

    Args:
        url: The target URL.
        method: HTTP method (GET or POST).
        body: Optional request body as a JSON string.
        show_headers: Whether to include response headers in the output.
        allow_private_networks: Allow private, loopback and link-local targets.

    Returns:
        A formatted string containing the status, headers (if requested), and body.
    """
    try:
        with _open(url, method, body, allow_private_networks) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "unknown")
            resp_body = resp.read().decode("utf-8", errors="replace")

            lines = [f"Status: {status}", f"Content-Type: {content_type}"]

            if show_headers:
                lines.append("Headers:")
                for key, value in resp.headers.items():
                    lines.append(f"  {key}: {value}")

            lines.append("")

            if "json" in content_type:
                try:
                    parsed = json.loads(resp_body)
                    resp_body = json.dumps(parsed, indent=2)
                except json.JSONDecodeError:
                    pass

            lines.append(resp_body[:5000])
            return "\n".join(lines)

    except HttpRequestBlockedError as e:
        return str(e)
    except urllib.error.HTTPError as e:
        body_text = (
            e.read().decode("utf-8", errors="replace")[:2000] if e.fp else ""
        )
        return f"HTTP Error: {e.code} {e.reason}\n{body_text}"
    except urllib.error.URLError as e:
        return f"Connection error: {e.reason}"
    except Exception as e:
        return f"Error: {e}"


class CliHttpCommandProcessor(CliCommandProcessor):
    """Command processor for making HTTP requests from the server.

    Only http(s) URLs are fetched, and targets (including every redirect hop)
    that resolve to a loopback, private, link-local, CGNAT or unspecified
    address are refused, so the command cannot reach cloud metadata or
    internal services.  ``allow_private_networks=True`` lifts the address
    check (the scheme check stays).
    """

    def __init__(self, allow_private_networks: bool = False) -> None:
        self._allow_private_networks = allow_private_networks

    @property
    def command(self) -> str:
        return "http"

    @property
    def description(self) -> str:
        return "Makes HTTP requests from the server"

    @property
    def allow_unlisted_commands(self) -> bool:
        return False

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return [
            _HttpGetProcessor(self._allow_private_networks),
            _HttpPostProcessor(self._allow_private_networks),
        ]

    async def handle_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> str:
        return "Usage: http get|post <url> [--body <json>] [--headers]"
