"""Admin dashboard color scheme: validation and GET /qcli/admin/theme.json."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qodalis_cli_admin import (
    ADMIN_THEME_TOKENS,
    AdminBuildDeps,
    CliAdminBuilder,
    admin_theme_json,
    is_theme_color,
    validate_admin_theme,
)


@pytest.mark.parametrize(
    "value",
    [
        "#fff", "#ffff", "#4f46e5", "#4f46e5cc", "rgb(1, 2, 3)", "rgba(255,255,255,0.04)",
        "rgb(1 2 3 / 50%)", "hsl(240deg 100% 50%)", "oklch(0.7 0.1 250)", "transparent", "rebeccapurple",
    ],
)
def test_accepts_plain_colors(value):
    assert is_theme_color(value)


@pytest.mark.parametrize(
    "value",
    [
        "", "#ggg", "#12345", "red;}body{display:none", "url(https://x)", "var(--x)",
        "rgb(1,2,3);color:red", "expression(alert(1))", '"red"', "rgb(calc(1),2,3)",
        "</style><script>", 42, None, "x" * 101,
    ],
)
def test_rejects_anything_else(value):
    assert not is_theme_color(value)


def test_default_is_dark_without_overrides():
    assert validate_admin_theme(None) == {"mode": "dark", "dark": {}, "light": {}}
    assert admin_theme_json(None) == b'{"mode":"dark","dark":{},"light":{}}'


def test_keeps_overrides_trimmed_and_accepts_snake_case():
    theme = validate_admin_theme(
        {"mode": "system", "light": {"primary": " #2a6bba "}, "dark": {"primary_hover": "rgb(19 33 47)"}}
    )
    assert theme == {
        "mode": "system",
        "dark": {"primaryHover": "rgb(19 33 47)"},
        "light": {"primary": "#2a6bba"},
    }


def test_accepts_every_token():
    colors = {token: "#000000" for token in ADMIN_THEME_TOKENS}
    assert validate_admin_theme({"dark": colors})["dark"] == colors
    assert len(ADMIN_THEME_TOKENS) == 21


@pytest.mark.parametrize(
    ("theme", "message"),
    [
        ({"mode": "neon"}, "mode"),
        ({"dark": {"brand": "#fff"}}, 'unknown token "dark.brand"'),
        ({"light": {"primary": "red;} body{display:none"}}, 'color for "light.primary"'),
        ({"dark": "#fff"}, '"dark" must be a mapping'),
        ({"colors": {}}, "unknown keys"),
    ],
)
def test_rejects_invalid_themes(theme, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(")):
        validate_admin_theme(theme)


def test_set_theme_fails_fast():
    with pytest.raises(ValueError):
        CliAdminBuilder().set_theme({"dark": {"error": "url(x)"}})


class _Stub:
    processors: list = []
    modules: list = []

    def get_clients(self) -> list:
        return []


def _dashboard_app(tmp_path, builder: CliAdminBuilder) -> FastAPI:
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    plugin = (
        builder.set_credentials("admin", "secret")
        .set_dashboard_path(str(tmp_path))
        .build(AdminBuildDeps(registry=_Stub(), event_socket_manager=_Stub(), builder=_Stub()))
    )
    app = FastAPI()
    app.mount(plugin.dashboard_prefix, plugin.dashboard_app)
    return app


async def _get(app: FastAPI, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


async def test_serves_configured_theme_without_auth(tmp_path):
    app = _dashboard_app(
        tmp_path, CliAdminBuilder().set_theme({"mode": "light", "light": {"primary": "#2a6bba"}})
    )
    res = await _get(app, "/qcli/admin/theme.json")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.headers["cache-control"] == "no-cache"
    assert res.json() == {"mode": "light", "dark": {}, "light": {"primary": "#2a6bba"}}


async def test_serves_defaults_and_keeps_spa_fallback(tmp_path):
    app = _dashboard_app(tmp_path, CliAdminBuilder())
    res = await _get(app, "/qcli/admin/theme.json")
    assert json.loads(res.content) == {"mode": "dark", "dark": {}, "light": {}}
    spa = await _get(app, "/qcli/admin/jobs")
    assert spa.status_code == 200
    assert "id=root" in spa.text
