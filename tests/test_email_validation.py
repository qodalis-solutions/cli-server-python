"""The email rule is shared with web-cli and the other servers (same cases)."""

from __future__ import annotations

import pytest

from qodalis_cli_users.validation import validate_email

VALID = [
    "a@x.io",
    "root@localhost",
    "first.last+tag@sub.example.co.uk",
    "o'neil@x.com",
    "x@xn--80ak6aa92e.com",
    "a_b-c@my-host.org",
    "ALICE@EXAMPLE.COM",
]
INVALID = [
    "Nicolae L",
    "nicolae",
    "a@b",
    "a@b.c",
    "a..b@x.io",
    ".a@x.io",
    "a.@x.io",
    "a@-x.io",
    "a@x-.io",
    "a@x_y.io",
    "a b@x.io",
    "a@x.io.",
    "a@@x.io",
    "a@x.123",
    "@x.io",
    "a@.x.io",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@x.io",
    "a@localhost.",
    "a@x..io",
    "",
    "a@x.io\n",
]


@pytest.mark.parametrize("email", VALID)
def test_accepts_valid_email(email: str) -> None:
    assert validate_email(email) == email


@pytest.mark.parametrize("email", INVALID)
def test_rejects_invalid_email(email: str) -> None:
    with pytest.raises(Exception):
        validate_email(email)
