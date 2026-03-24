"""Parser for human-readable interval strings (e.g. ``30s``, ``5m``)."""

from __future__ import annotations

import re

_PATTERN = re.compile(r"^(\d+)(s|m|h|d)$")

_MULTIPLIERS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def parse_interval(value: str) -> float:
    """Parse an interval string like ``"30s"``, ``"5m"``, ``"1h"``, ``"1d"``
    and return the number of **seconds** as a float.

    Raises ``ValueError`` on invalid format.
    """
    match = _PATTERN.match(value.strip())
    if not match:
        raise ValueError(
            f"Invalid interval format: {value!r}. "
            "Expected a number followed by s, m, h, or d (e.g. '30s', '5m')."
        )
    amount = int(match.group(1))
    unit = match.group(2)
    return amount * _MULTIPLIERS[unit]
