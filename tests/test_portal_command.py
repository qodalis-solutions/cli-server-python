"""The portal plugin's ``portal`` config provider and ``portal`` command: the builder's branding as defaults,
stored values overriding them live in /portal.json, validation errors, the read-only site section; the command's
text (site found or missing, with and without sections), --json, and config:read on a protected server.

Ported from cli-server-node ``src/__tests__/portal-command.test.ts``."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_docs import CliDocsBuilder
from qodalis_cli_portal import SITE_NOT_FOUND, CliPortalBuilder
from qodalis_cli_server_abstractions.config import CliConfigError, InMemoryConfigStore, cli_config_context

from tests.command_helpers import add_users, protect, run_command

SITE = Path(__file__).parent / "fixtures" / "portal-site"
SITE_DIR = str(SITE.resolve())
DOCS = Path(__file__).parent / "fixtures" / "docs"
HINT = "Change the branding with config set portal.branding.<key> <value> (name, logoUrl, themeMode, footerLinks)."
LINKS_SHAPE = 'must be a list of { "label": ..., "url": ... } links'
LOGO_TEXT = "portal logo must be an http(s) URL, a root-relative path or a data:image URL"
# The config service (the admin plugin serves it as GET/PATCH /api/v1/qcli/config), as an open server's caller
ALL = cli_config_context(None, "api")


class Srv:
    def __init__(self, client: TestClient, result: Any, portal: Any) -> None:
        self.c, self.result, self.portal = client, result, portal

    def run(self, line: str, token: Optional[str] = None) -> Any:
        return run_command(self.c, self.result, line, token)

    def text(self, line: str, token: Optional[str] = None) -> str:
        return self.run(line, token).text

    def portal_json(self) -> dict[str, Any]:
        res = self.c.get("/qcli/portal/portal.json")
        assert res.status_code == 200
        return res.json()

    def section(self, section_id: str) -> dict[str, Any]:
        view = self.c.portal.call(self.result.config.get, "portal", ALL)
        return next(s for s in view["sections"] if s["id"] == section_id)

    def patch(self, changes: dict[str, Any], section_id: str = "branding") -> tuple[int, dict[str, Any]]:
        try:
            body = self.c.portal.call(self.result.config.update, "portal", section_id, {"changes": changes}, ALL)
            return 200, body
        except CliConfigError as exc:
            return exc.status, {"error": exc.message, "errors": exc.errors}

    def ok(self, changes: dict[str, Any]) -> dict[str, Any]:
        status, body = self.patch(changes)
        assert body.get("errors") is None, body
        assert status == 200
        return body


def entry(section: dict[str, Any], key: str) -> dict[str, Any]:
    return next(e for e in section["entries"] if e["key"] == key)


@contextmanager
def start(
    sections: bool = True,
    site_dir: str = str(SITE),
    configure: Optional[Callable[[CliPortalBuilder], Any]] = None,
    store: Any = None,
    builder: Optional[CliPortalBuilder] = None,
    protected: bool = False,
) -> Iterator[Srv]:
    """A server with the portal (stand-in site), optionally the docs (3 pages) and public-api sections."""
    options = CliServerOptions(configure=(lambda b: b.set_config_store(store)) if store is not None else None)
    result = create_cli_server(options)
    if protected:
        protect(result)
    if builder is None:
        builder = CliPortalBuilder().set_site_dir(site_dir).set_name("RateBell API").add_footer_link("Terms", "/terms")
        if configure is not None:
            configure(builder)
        if sections:
            docs = CliDocsBuilder().add_folder(str(DOCS)).build()
            result.mount_plugin(docs)
            # The public-api plugin's section (the plugin itself needs the users plugin; portal.json only reads it)
            builder.add_section(docs).add_section({"kind": "api", "title": "API reference", "base_path": "/qcli/api"})
    portal = builder.build()
    result.mount_plugin(portal)
    with TestClient(result.app) as client:
        yield Srv(client, result, portal)


# ---------------------------------------------------------------------------
# The portal config provider
# ---------------------------------------------------------------------------


class TestPortalConfigProvider:
    def test_branding_the_builder_values_are_the_defaults(self):
        with start(configure=lambda b: b.set_logo_url("/logo.svg").set_theme({"mode": "dark"})) as srv:
            branding = srv.section("branding")
            assert [
                [e["key"], e["type"], e["value"], e["default"], e["source"], e["mutable"]] for e in branding["entries"]
            ] == [
                ["name", "string", "RateBell API", "RateBell API", "default", True],
                ["logoUrl", "string", "/logo.svg", "/logo.svg", "default", True],
                ["themeMode", "enum", "dark", "dark", "default", True],
                ["footerLinks", "json", [{"label": "Terms", "url": "/terms"}], [{"label": "Terms", "url": "/terms"}],
                 "default", True],
            ]
            assert entry(branding, "themeMode")["validation"] == {"options": ["system", "light", "dark"]}
            assert branding["version"] == "0"

    def test_branding_unset_builder_values_default_to_documentation_no_logo_system_no_links(self):
        with start(builder=CliPortalBuilder().set_site_dir(str(SITE))) as srv:
            assert [e["value"] for e in srv.section("branding")["entries"]] == ["Documentation", None, "system", []]

    def test_site_path_site_found_and_sections_read_only(self):
        with start() as srv:
            site = srv.section("site")
            assert [[e["key"], e["value"], e["mutable"]] for e in site["entries"]] == [
                ["path", "/qcli/portal", False],
                ["siteFound", True, False],
                ["sections", ["docs", "api"], False],
            ]
            assert site["version"] is None
            assert srv.patch({"path": "/x"}, "site") == (
                400, {"error": "config section portal.site is read-only", "errors": None},
            )

    def test_stored_values_override_the_defaults_and_apply_live_to_portal_json_no_restart(self):
        with start(configure=lambda b: b.set_theme({"mode": "system", "light": {"primary": "#0a7"}})) as srv:
            assert srv.portal_json()["name"] == "RateBell API"
            status, body = srv.patch({
                "name": "Acme Docs",
                "logoUrl": "https://cdn.example.com/logo.png",
                "themeMode": "dark",
                "footerLinks": [
                    {"label": "Privacy", "url": "https://example.com/privacy"},
                    {"label": "Status", "url": "/status"},
                ],
            })
            assert status == 200
            assert body["applied"] == ["name", "logoUrl", "themeMode", "footerLinks"]
            assert body["requiresRestart"] is False
            doc = srv.portal_json()
            assert doc["name"] == "Acme Docs"
            assert doc["logo"] == "https://cdn.example.com/logo.png"
            assert doc["theme"] == {"mode": "dark", "dark": {}, "light": {"primary": "#0a7"}}
            assert doc["footerLinks"] == [
                {"label": "Privacy", "url": "https://example.com/privacy"},
                {"label": "Status", "url": "/status"},
            ]
            assert doc["docs"] == {"title": "Guides", "pages": "/qcli/docs/pages"}
            name = entry(srv.section("branding"), "name")
            assert (name["value"], name["default"], name["source"]) == ("Acme Docs", "RateBell API", "runtime")

    def test_values_already_in_the_config_store_override_the_defaults_from_startup(self):
        import asyncio

        store = InMemoryConfigStore()
        asyncio.run(store.write("portal", "branding", {
            "name": "Stored Name", "themeMode": "light", "footerLinks": [{"label": "Blog", "url": "/blog"}],
        }))
        builder = CliPortalBuilder().set_site_dir(str(SITE)).set_name("RateBell API")
        with start(builder=builder, store=store) as srv:
            srv.c.portal.call(srv.result.config.ready)
            doc = srv.portal_json()
            assert doc["name"] == "Stored Name"
            assert doc["theme"]["mode"] == "light"
            assert doc["footerLinks"] == [{"label": "Blog", "url": "/blog"}]
            name = entry(srv.section("branding"), "name")
            assert (name["value"], name["default"]) == ("Stored Name", "RateBell API")

    def test_logo_url_empty_removes_the_logo_a_reset_brings_the_builder_value_back(self):
        with start(configure=lambda b: b.set_logo_url("/logo.svg")) as srv:
            srv.ok({"logoUrl": ""})
            assert srv.portal_json()["logo"] is None
            srv.ok({"logoUrl": "data:image/png;base64,iVBORw0KGgo="})
            assert srv.portal_json()["logo"] == "data:image/png;base64,iVBORw0KGgo="
            srv.ok({"logoUrl": None, "name": "Other", "footerLinks": []})
            assert srv.portal_json()["footerLinks"] == []
            srv.ok({"name": None, "footerLinks": None})
            doc = srv.portal_json()
            assert doc["name"] == "RateBell API"
            assert doc["logo"] == "/logo.svg"
            assert doc["footerLinks"] == [{"label": "Terms", "url": "/terms"}]

    def test_config_set_portal_branding_key_changes_the_site_live(self):
        with start() as srv:
            assert srv.text('config set portal.branding.name "Acme Docs"') == (
                "Updated portal.branding.name = Acme Docs (version 1)"
            )
            assert srv.text("config set portal.branding.themeMode light") == (
                "Updated portal.branding.themeMode = light (version 2)"
            )
            text = srv.text(
                """config set portal.branding.footerLinks '[{"label":"Blog","url":"https://example.com/blog"}]'"""
            )
            assert text.startswith("Updated portal.branding.footerLinks = "), text
            doc = srv.portal_json()
            assert doc["name"] == "Acme Docs"
            assert doc["theme"]["mode"] == "light"
            assert doc["footerLinks"] == [{"label": "Blog", "url": "https://example.com/blog"}]

    def test_validation_per_key_errors_nothing_applied(self):
        with start() as srv:
            def errors(changes: dict[str, Any]) -> Any:
                status, body = srv.patch(changes)
                assert status == 400, body
                return body["errors"]

            too_many = [{"label": f"L{i}", "url": "/x"} for i in range(21)]
            assert errors({"name": ""}) == {"name": "must be at least 1 characters"}
            assert errors({"name": "x" * 81}) == {"name": "must be at most 80 characters"}
            assert errors({"name": "a\nb"}) == {
                "name": "portal name must be 1-80 characters without control characters",
            }
            assert errors({"logoUrl": "javascript:alert(1)"}) == {"logoUrl": LOGO_TEXT}
            assert errors({"logoUrl": "//evil.example/logo.png"}) == {"logoUrl": LOGO_TEXT}
            assert errors({"themeMode": "blue"}) == {"themeMode": "must be one of: system, light, dark"}
            assert errors({"footerLinks": {"label": "x", "url": "/x"}}) == {"footerLinks": LINKS_SHAPE}
            assert errors({"footerLinks": ["x"]}) == {"footerLinks": LINKS_SHAPE}
            assert errors({"footerLinks": too_many}) == {"footerLinks": "must have at most 20 links"}
            assert errors({"footerLinks": [{"label": "Bad", "url": "ftp://x"}]}) == {
                "footerLinks": "invalid portal footer link: Bad",
            }
            assert errors({"footerLinks": [{"label": "", "url": "/x"}]}) == {
                "footerLinks": "invalid portal footer link: ",
            }
            assert srv.text("config set portal.branding.themeMode blue") == (
                "Error: validation failed\n  themeMode: must be one of: system, light, dark"
            )
            assert srv.portal_json()["name"] == "RateBell API"


# ---------------------------------------------------------------------------
# The portal command
# ---------------------------------------------------------------------------


def _expected(logo: str = "/logo.svg") -> str:
    return "\n".join([
        "Portal: /qcli/portal",
        f"Site: found, set_site_dir ({SITE_DIR})",
        "Sections:",
        "  docs  Guides, 3 pages (/qcli/docs/pages)",
        "  api   API reference (/qcli/api/openapi.json)",
        "Branding:",
        "  name         RateBell API",
        f"  logoUrl      {logo}",
        "  themeMode    system",
        "  footerLinks  Terms (/terms)",
        "",
        HINT,
    ])


class TestPortalCommand:
    def test_is_registered_under_the_portal_module_with_status_needing_config_read(self):
        with start() as srv:
            processor = srv.result.registry.find_processor("portal")
            assert processor is not None
            assert list(processor.required_permissions) == ["config:read"]
            assert [p.command for p in processor.processors] == ["status"]
            assert processor.module == "portal"
            assert srv.result.registry.module_of(processor) == "portal"
            assert "config set portal.branding.<key>" in processor.description

    def test_portal_and_portal_status_path_site_sections_with_the_page_count_and_the_api_branding(self):
        with start(configure=lambda b: b.set_logo_url("/logo.svg")) as srv:
            assert srv.text("portal") == _expected()
            assert srv.text("portal status") == _expected()

    def test_shows_the_current_branding_after_config_set(self):
        with start() as srv:
            srv.text("config set portal.branding.name Acme")
            srv.text("config set portal.branding.logoUrl data:image/png;base64,iVBORw0KGgo=")
            text = srv.text("portal")
            assert "  name         Acme\n" in text
            assert "  logoUrl      data:image/png;base64,… (34 characters)\n" in text

    def test_without_sections_or_footer_links(self):
        with start(builder=CliPortalBuilder().set_site_dir(str(SITE)).set_path("/help")) as srv:
            assert srv.text("portal") == "\n".join([
                "Portal: /help",
                f"Site: found, set_site_dir ({SITE_DIR})",
                'Sections: none (the site shows "Nothing published yet")',
                "Branding:",
                "  name         Documentation",
                "  logoUrl      (none)",
                "  themeMode    system",
                "  footerLinks  (none)",
                "",
                HINT,
            ])

    def test_site_missing_says_the_portal_is_not_served_the_provider_shows_site_found_false(self, caplog):
        with caplog.at_level(logging.WARNING):
            with start(site_dir=str(Path(__file__).parent / "fixtures" / "no-such-site")) as srv:
                assert SITE_NOT_FOUND in caplog.text
                text = srv.text("portal")
                assert text.split("\n")[:2] == [
                    "Portal: /qcli/portal",
                    "Site: not found, the portal is not served "
                    "(install @qodalis/cli-server-portal or call set_site_dir)",
                ]
                assert "  docs  Guides, 3 pages (/qcli/docs/pages)" in text
                assert entry(srv.section("site"), "siteFound")["value"] is False
                status = json.loads(srv.text("portal --json"))
                assert status["site"] == {"path": "/qcli/portal", "found": False, "source": None, "dir": None}

    def test_json_portal_json_with_the_page_count_and_the_site(self):
        with start() as srv:
            srv.text("config set portal.branding.themeMode dark")
            status = json.loads(srv.text("portal --json"))
            assert status == {
                "name": "RateBell API",
                "logo": None,
                "theme": {"mode": "dark", "dark": {}, "light": {}},
                "footerLinks": [{"label": "Terms", "url": "/terms"}],
                "docs": {"title": "Guides", "pages": "/qcli/docs/pages", "pageCount": 3},
                "api": {"title": "API reference", "openapi": "/qcli/api/openapi.json", "swagger": "/qcli/api/swagger"},
                "site": {"path": "/qcli/portal", "found": True, "source": "siteDir", "dir": SITE_DIR},
            }
            assert json.loads(srv.text("portal status --json")) == status
            # The same as /portal.json, but for pageCount and site
            rest = {k: v for k, v in status.items() if k != "site"}
            rest["docs"] = {k: v for k, v in status["docs"].items() if k != "pageCount"}
            assert srv.portal_json() == rest

    def test_on_a_protected_server_config_read_runs_it_commands_execute_alone_is_refused(self):
        with start(protected=True) as srv:
            tokens = add_users(srv.c, {
                "runner": ["commands:execute"], "reader": ["commands:execute", "config:read"],
            })
            assert srv.run("portal").status == 401
            assert srv.run("portal", tokens["runner"]).status == 403
            ok = srv.run("portal status", tokens["reader"])
            assert ok.status == 200
            assert ok.text.split("\n")[0] == "Portal: /qcli/portal"
            assert "  name         RateBell API" in srv.text("portal", tokens["root"])
            # Changing the branding needs config:write
            assert srv.run("config set portal.branding.name Nope", tokens["reader"]).status == 403
            assert srv.text("config set portal.branding.name Acme", tokens["root"]) == (
                "Updated portal.branding.name = Acme (version 1)"
            )
            # The portal itself stays public
            res = srv.c.get("/qcli/portal/portal.json")
            assert res.status_code == 200
            assert res.json()["name"] == "Acme"
