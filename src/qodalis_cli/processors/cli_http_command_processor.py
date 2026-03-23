from __future__ import annotations

import asyncio
import json
import urllib.request
import urllib.error

from qodalis_cli_server_abstractions import ICliStreamCommandProcessor

from ..abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)


class _HttpGetProcessor(CliCommandProcessor, ICliStreamCommandProcessor):
    """Sub-processor that performs HTTP GET requests."""

    @property
    def command(self) -> str:
        return "get"

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
        url = command.value
        if not url:
            return "Usage: http get <url>"
        return _do_request(url, method="GET", show_headers="headers" in command.args)

    async def handle_stream_async(
        self,
        command: CliProcessCommand,
        emit,
        cancellation_event: asyncio.Event | None = None,
    ) -> int:
        url = command.value
        if not url:
            emit({"type": "text", "value": "Usage: http get <url>"})
            return 1
        return await _do_stream_request(url, "GET", None, "headers" in command.args, emit, cancellation_event)


class _HttpPostProcessor(CliCommandProcessor, ICliStreamCommandProcessor):
    """Sub-processor that performs HTTP POST requests."""

    @property
    def command(self) -> str:
        return "post"

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
        url = command.value
        if not url:
            return "Usage: http post <url> --body '{\"key\":\"value\"}'"
        body = command.args.get("body")
        return _do_request(
            url, method="POST", body=body, show_headers="headers" in command.args
        )

    async def handle_stream_async(
        self,
        command: CliProcessCommand,
        emit,
        cancellation_event: asyncio.Event | None = None,
    ) -> int:
        url = command.value
        if not url:
            emit({"type": "text", "value": "Usage: http post <url> --body '{\"key\":\"value\"}'"})
            return 1
        body = command.args.get("body")
        return await _do_stream_request(url, "POST", body, "headers" in command.args, emit, cancellation_event)


async def _do_stream_request(
    url: str,
    method: str,
    body: str | None,
    show_headers: bool,
    emit,
    cancellation_event: asyncio.Event | None = None,
) -> int:
    try:
        if cancellation_event and cancellation_event.is_set():
            return 1
        emit({"type": "text", "value": f"Fetching {method} {url}...", "style": "info"})

        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        if body:
            req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=30) as resp:
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
) -> str:
    """Perform an HTTP request and return a formatted text summary.

    Args:
        url: The target URL.
        method: HTTP method (GET or POST).
        body: Optional request body as a JSON string.
        show_headers: Whether to include response headers in the output.

    Returns:
        A formatted string containing the status, headers (if requested), and body.
    """
    try:
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        if body:
            req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=30) as resp:
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
    """Command processor for making HTTP requests from the server."""

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
        return [_HttpGetProcessor(), _HttpPostProcessor()]

    async def handle_async(
        self,
        command: CliProcessCommand,
        cancellation_event: asyncio.Event | None = None,
    ) -> str:
        return "Usage: http get|post <url> [--body <json>] [--headers]"
