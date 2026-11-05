"""Admin dashboard name and icon: validation and the ``branding`` of theme.json."""

from __future__ import annotations

import pytest

from qodalis_cli_admin import CliAdminBuilder, is_branding_icon, validate_admin_branding
from tests.test_admin_theme import _dashboard_app, _get


def test_unset_branding_is_left_out():
    assert validate_admin_branding(None) is None
    assert validate_admin_branding({"name": "  ", "icon_url": ""}) is None


def test_keeps_name_icon_and_terminal_name():
    assert validate_admin_branding(
        {"name": " RateBell ", "iconUrl": "/favicon.svg", "cli_name": "RateBell CLI"}
    ) == {"name": "RateBell", "icon": "/favicon.svg", "cli": {"name": "RateBell CLI"}}


@pytest.mark.parametrize(
    "icon", ["/favicon.svg", "https://ratebell.app/favicon.svg", "data:image/png;base64,iVBORw0KGgo="]
)
def test_accepts_icons(icon):
    assert is_branding_icon(icon)


@pytest.mark.parametrize(
    "icon",
    [
        "//evil.example/x.svg",
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "data:image/svg+xml,<svg onload=alert(1)>",
        "favicon.svg",
        "/a b.svg",
    ],
)
def test_rejects_other_icons(icon):
    with pytest.raises(ValueError, match="icon_url"):
        validate_admin_branding({"icon_url": icon})


def test_rejects_long_names_and_control_characters():
    with pytest.raises(ValueError, match="name"):
        validate_admin_branding({"name": "a" * 81})
    with pytest.raises(ValueError, match="cli_name"):
        validate_admin_branding({"cli_name": "a\u0007b"})


async def test_branding_is_served_in_theme_json(tmp_path):
    app = _dashboard_app(tmp_path, CliAdminBuilder().set_branding({"name": "RateBell", "cli_name": "RateBell CLI"}))
    res = await _get(app, "/qcli/admin/theme.json")
    assert res.json() == {
        "mode": "dark",
        "dark": {},
        "light": {},
        "branding": {"name": "RateBell", "cli": {"name": "RateBell CLI"}},
    }
