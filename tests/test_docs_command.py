"""The docs plugin's ``docs`` terminal command through POST /execute: list (grouped, --group, --json), read
(markdown with variables, --json, heading from the title), no pages, unknown or missing slugs, and a protected
server (no permission beyond commands:execute, like the public pages)."""

from __future__ import annotations

import json
from typing import Callable

from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_docs import CliDocsBuilder

from .command_helpers import add_users, protect, run_command, sub_permissions


def with_pages(b: CliDocsBuilder) -> CliDocsBuilder:
    return (
        b.add_page("intro", "---\ntitle: Welcome to {{name}}\norder: 1\n---\nWrite to {{support}}. API at {{baseUrl}}/api.")
        .add_page("auth", "---\ntitle: Authentication\norder: 2\n---\n# Sign in\n\nUse a token.")
        .add_page("limits", "---\ntitle: Rate limits\ngroup: Reference\norder: 10\n---\n")
        .add_page("errors", "---\ntitle: Errors\ngroup: Reference\norder: 10\n---\n{{unknown}} stays.")
    )


def start(configure: Callable[[CliDocsBuilder], object] = with_pages):
    builder = CliDocsBuilder().set_name("RateBell API").set_variable("support", "help@ratebell.app")
    configure(builder)
    result = create_cli_server()
    result.mount_plugin(builder.build())
    return result


class Cli:
    def __init__(self, configure: Callable[[CliDocsBuilder], object] = with_pages) -> None:
        self.result = start(configure)
        self.client = TestClient(self.result.app)

    def text(self, line: str, token: str | None = None) -> str:
        return run_command(self.client, self.result, line, token).text


EXPECTED = "\n".join([
    "Guides", "  intro   Welcome to RateBell API", "  auth    Authentication", "",
    "Reference", "  errors  Errors", "  limits  Rate limits",
])


def test_registered_with_list_and_read_needing_no_permission_beyond_commands_execute():
    result = start()
    assert result.registry.find_processor("docs").required_permissions is None
    assert sub_permissions(result, "docs") == {"list": None, "read": None}


def test_docs_and_docs_list_the_pages_by_group():
    cli = Cli()
    assert cli.text("docs") == EXPECTED
    assert cli.text("docs list") == EXPECTED
    assert cli.text("docs ls") == EXPECTED


def test_docs_list_group_filters_and_json_prints_the_list():
    cli = Cli()
    assert cli.text("docs list --group reference") == "Reference\n  errors  Errors\n  limits  Rate limits"
    assert cli.text("docs list --group Nope") == "No documentation pages in the group Nope."
    assert cli.text("docs list --group") == "Error: --group needs a group name"
    items = json.loads(cli.text("docs list --json"))
    assert items[0] == {"slug": "intro", "title": "Welcome to RateBell API", "group": "Guides", "order": 1}
    assert len(items) == 4


def test_lists_a_group_once_even_when_its_pages_are_not_adjacent():
    cli = Cli(lambda b: b.add_page("a", "---\ntitle: A\norder: 1\n---\n")
              .add_page("b", "---\ntitle: B\ngroup: Other\norder: 2\n---\n")
              .add_page("c", "---\ntitle: C\norder: 3\n---\n"))
    assert cli.text("docs") == "Guides\n  a  A\n  c  C\n\nOther\n  b  B"


def test_says_so_when_there_are_no_pages():
    cli = Cli(lambda b: None)
    assert cli.text("docs") == "No documentation pages."
    assert cli.text("docs list --json") == "[]"


def test_docs_read_markdown_with_variables_and_title_heading():
    cli = Cli()
    assert cli.text("docs read intro") == "# Welcome to RateBell API\n\nWrite to help@ratebell.app. API at /api."
    assert cli.text("docs read auth") == "# Sign in\n\nUse a token."
    assert cli.text("docs read limits") == "# Rate limits"
    assert cli.text("docs show errors") == "# Errors\n\n{{unknown}} stays."


def test_docs_read_uses_the_configured_base_url():
    cli = Cli(lambda b: with_pages(b).set_base_url("https://api.example.com/"))
    assert "API at https://api.example.com/api." in cli.text("docs read intro")


def test_docs_read_json_the_page_as_get_pages_serves_it():
    cli = Cli()
    assert json.loads(cli.text("docs read intro --json")) == {
        "slug": "intro", "title": "Welcome to RateBell API", "group": "Guides", "order": 1,
        "markdown": "Write to help@ratebell.app. API at /api.",
    }


def test_docs_read_missing_unknown_or_several_slugs():
    cli = Cli()
    assert cli.text("docs read") == "Error: name the page: docs read <slug> (docs list shows them)"
    assert cli.text("docs read nope") == "Error: page not found: nope (docs list shows the pages)"
    assert cli.text("docs read intro auth") == "Error: one page at a time: docs read <slug>"


def test_protected_server_commands_execute_is_enough_anonymous_refused():
    result = start()
    protect(result)
    with TestClient(result.app) as client:
        tokens = add_users(client, {"runner": ["commands:execute"]})
        assert run_command(client, result, "docs list").status == 401
        assert run_command(client, result, "docs read auth", tokens["runner"]).text == "# Sign in\n\nUse a token."
