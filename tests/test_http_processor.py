import asyncio

import pytest

from qodalis_cli.processors.cli_http_command_processor import (
    _do_request,
    _do_stream_request,
)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/hosts", "FILE:///etc/hosts", "ftp://example.com/x", "data:text/plain,hi", "/etc/hosts"],
)
def test_request_rejects_non_http_schemes(url):
    assert "Unsupported URL scheme" in _do_request(url)


def test_stream_request_rejects_file_scheme():
    events = []
    code = asyncio.run(_do_stream_request("file:///etc/hosts", "GET", None, False, events.append))
    assert code == 1
    assert len(events) == 1
    assert "Unsupported URL scheme" in events[0]["value"]
