"""SSRF guard of the ``http`` command: private/local targets and redirect hops."""

import asyncio
import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from qodalis_cli.abstractions import CliProcessCommand
from qodalis_cli.processors import cli_http_command_processor as http_module
from qodalis_cli.processors.cli_http_command_processor import (
    CliHttpCommandProcessor,
    _do_request,
    _do_stream_request,
)


def _fake_dns(monkeypatch, table):
    """Resolve hostnames from ``table`` instead of the network."""

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in table[host]
        ]

    monkeypatch.setattr(http_module.socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "url, ip",
    [
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("http://127.0.0.1:8048/api/qcli/fs/ls", "127.0.0.1"),
        ("http://10.1.2.3/", "10.1.2.3"),
        ("http://172.20.0.1/", "172.20.0.1"),
        ("http://192.168.1.1/", "192.168.1.1"),
        ("http://100.64.0.1/", "100.64.0.1"),
        ("http://0.0.0.0/", "0.0.0.0"),
        ("http://[::1]/", "::1"),
        ("http://[::]/", "::"),
        ("http://[fd00::1]/", "fd00::1"),
        ("http://[fe80::1]/", "fe80::1"),
        ("http://[::ffff:127.0.0.1]/", "::ffff:7f00:1"),
        ("http://[::ffff:169.254.169.254]/", "::ffff:a9fe:a9fe"),
    ],
)
def test_ip_literals_are_blocked(url, ip):
    host = ipaddress.ip_address(ip)
    result = _do_request(url)
    assert result == (
        f"Blocked: {host} resolves to a private or local address ({host}). "
        "Set allow_private_networks to allow it."
    )


def test_hostname_with_any_private_address_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"mixed.example": ["93.184.216.34", "10.0.0.5"]})
    result = _do_request("http://mixed.example/")
    assert result.startswith("Blocked: mixed.example resolves to a private or local address (10.0.0.5).")


def test_localhost_name_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"localhost": ["127.0.0.1", "::1"]})
    assert _do_request("http://localhost:8048/").startswith("Blocked: localhost")


def test_stream_request_is_blocked():
    events = []
    code = asyncio.run(
        _do_stream_request("http://169.254.169.254/", "GET", None, False, events.append)
    )
    assert code == 1
    assert events[-1]["style"] == "error"
    assert events[-1]["value"].startswith("Blocked: 169.254.169.254")


def test_non_http_scheme_is_refused():
    assert "Unsupported URL scheme 'file'" in _do_request("file:///etc/hosts")


def test_processor_blocks_by_default():
    processor = CliHttpCommandProcessor()
    get = next(p for p in processor.processors if p.command == "get")
    command = CliProcessCommand(
        command="http", chain_commands=["get"], raw_command="http get http://169.254.169.254/",
        value="http://169.254.169.254/", args={},
    )
    assert asyncio.run(get.handle_async(command)).startswith("Blocked: 169.254.169.254")


# --- end-to-end against a local server -------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path.startswith("/hop/"):
            n = int(self.path.rsplit("/", 1)[1])
            self.send_response(302)
            self.send_header("Location", "/ok" if n == 0 else f"/hop/{n - 1}")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_loopback_server_is_blocked_by_default(local_server):
    assert _do_request(f"{local_server}/ok").startswith("Blocked: 127.0.0.1")


def test_allow_private_networks_restores_access(local_server):
    result = _do_request(f"{local_server}/ok", allow_private_networks=True)
    assert "Status: 200" in result
    assert "hello" in result


def test_processor_option_allows_private_networks(local_server):
    processor = CliHttpCommandProcessor(allow_private_networks=True)
    get = next(p for p in processor.processors if p.command == "get")
    command = CliProcessCommand(
        command="http", chain_commands=["get"], raw_command=f"http get {local_server}/ok",
        value=f"{local_server}/ok", args={},
    )
    assert "hello" in asyncio.run(get.handle_async(command))


@pytest.fixture
def loopback_allowed(monkeypatch):
    """Treat 127.0.0.0/8 as public so the local server can play an external host."""
    monkeypatch.setattr(
        http_module,
        "_BLOCKED_NETWORKS",
        [n for n in http_module._BLOCKED_NETWORKS if str(n) != "127.0.0.0/8"],
    )


def test_redirect_to_private_address_is_blocked(local_server, loopback_allowed):
    result = _do_request(f"{local_server}/to-metadata")
    assert result.startswith("Blocked: 169.254.169.254 resolves to a private or local address")


def test_redirect_to_private_address_is_blocked_when_streaming(local_server, loopback_allowed):
    events = []
    code = asyncio.run(
        _do_stream_request(f"{local_server}/to-metadata", "GET", None, False, events.append)
    )
    assert code == 1
    assert events[-1]["value"].startswith("Blocked: 169.254.169.254")


def test_follows_up_to_five_redirects(local_server, loopback_allowed):
    result = _do_request(f"{local_server}/hop/4")  # 5 redirects
    assert "Status: 200" in result
    assert "hello" in result


def test_more_than_five_redirects_fail(local_server, loopback_allowed):
    result = _do_request(f"{local_server}/hop/5")  # 6 redirects
    assert "Status: 200" not in result
    assert result.startswith("HTTP Error: 302")
