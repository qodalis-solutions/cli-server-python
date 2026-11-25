"""The demo app's example commands (``demo/processors``), for the tests that run them: echo, status,
system, hash and base64 live in the demo, not in ``qodalis_cli``."""

from __future__ import annotations

import os
import sys

_DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo")
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

from processors import (  # noqa: E402
    CliBase64CommandProcessor,
    CliEchoCommandProcessor,
    CliHashCommandProcessor,
    CliStatusCommandProcessor,
    CliSystemCommandProcessor,
)

__all__ = [
    "CliBase64CommandProcessor",
    "CliEchoCommandProcessor",
    "CliHashCommandProcessor",
    "CliStatusCommandProcessor",
    "CliSystemCommandProcessor",
]
