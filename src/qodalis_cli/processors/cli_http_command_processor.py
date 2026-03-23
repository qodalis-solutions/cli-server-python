from __future__ import annotations

import json
import urllib.request
import urllib.error

from ..abstractions import (
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandParameterDescriptor,
    ICliCommandProcessor,
)


class _HttpGetProcessor(CliCommandProcessor):
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

    async def handle_async(self, command: CliProcessCommand) -> str:
        url = command.value
        if not url:
            return "Usage: http get <url>"
        return _do_request(url, method="GET", show_headers="headers" in command.args)


class _HttpPostProcessor(CliCommandProcessor):
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

    async def handle_async(self, command: CliProcessCommand) -> str:
        url = command.value
        if not url:
            return "Usage: http post <url> --body '{\"key\":\"value\"}'"
        body = command.args.get("body")
        return _do_request(
            url, method="POST", body=body, show_headers="headers" in command.args
        )


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

    async def handle_async(self, command: CliProcessCommand) -> str:
        return "Usage: http get|post <url> [--body <json>] [--headers]"
