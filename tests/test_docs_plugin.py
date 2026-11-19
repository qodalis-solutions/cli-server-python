"""The docs plugin: markdown guides from folders or code, front matter and
variables, served as JSON (contracts C7 and C9)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import create_cli_server
from qodalis_cli_docs import CliDocsBuilder, DocsPage, parse_page

FIXTURE = Path(__file__).parent / "fixtures" / "docs"

PAGES = [
    {"slug": "limits", "title": "Limits and quotas", "group": "Guides", "order": 0},
    {"slug": "index", "title": "Welcome to RateBell API", "group": "Guides", "order": 1},
    {"slug": "auth", "title": "Authentication", "group": "Basics", "order": 2},
]


def _builder() -> CliDocsBuilder:
    return (
        CliDocsBuilder()
        .set_name("RateBell API")
        .set_variable("support", "help@ratebell.app")
        .add_folder(str(FIXTURE))
    )


@contextmanager
def _client(configure: Callable[[CliDocsBuilder], Any] | None = None) -> Iterator[TestClient]:
    builder = _builder()
    if configure is not None:
        configure(builder)
    result = create_cli_server()
    result.mount_plugin(builder.build())
    with TestClient(result.app, base_url="http://localhost") as client:
        yield client


def test_pages_list_in_order():
    with _client() as client:
        res = client.get("/qcli/docs/pages")
        assert res.status_code == 200
        assert res.json() == PAGES
        assert [list(p) for p in res.json()] == [["slug", "title", "group", "order"]] * 3
        assert res.headers["cache-control"] == "no-cache"


def test_page_replaces_variables():
    with _client() as client:
        res = client.get("/qcli/docs/pages/index")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-cache"
        assert res.json() == {
            "slug": "index",
            "title": "Welcome to RateBell API",
            "group": "Guides",
            "order": 1,
            "markdown": "Call http://localhost/v1/rates with help@ratebell.app and {{unknown}}.\n",
        }


def test_page_front_matter_removed_heading_kept():
    with _client() as client:
        page = client.get("/qcli/docs/pages/auth").json()
        assert page["markdown"] == "# Ignored heading\nSend `Authorization: Bearer <token>`.\n"
        assert (page["title"], page["group"], page["order"]) == ("Authentication", "Basics", 2)


def test_base_url_option():
    with _client(lambda b: b.set_base_url("https://api.ratebell.app/")) as client:
        markdown = client.get("/qcli/docs/pages/index").json()["markdown"]
        assert markdown.startswith("Call https://api.ratebell.app/v1/rates")


def test_base_url_from_the_request():
    builder = _builder()
    result = create_cli_server()
    result.mount_plugin(builder.build())
    with TestClient(result.app, base_url="https://docs.example.com:8443") as client:
        markdown = client.get("/qcli/docs/pages/index").json()["markdown"]
        assert markdown.startswith("Call https://docs.example.com:8443/v1/rates")


def test_default_name():
    result = create_cli_server()
    result.mount_plugin(CliDocsBuilder().add_folder(str(FIXTURE)).build())
    with TestClient(result.app) as client:
        assert client.get("/qcli/docs/pages/index").json()["title"] == "Welcome to Documentation"
        # No host variable: left as written.
        assert "{{support}}" in client.get("/qcli/docs/pages/index").json()["markdown"]


@pytest.mark.parametrize("slug", ["nope", "INDEX", "..%2Fsecret", "index.md", "index/x", "%2E%2E"])
def test_unknown_slug_404(slug: str):
    with _client() as client:
        res = client.get(f"/qcli/docs/pages/{slug}")
        assert res.status_code == 404
        assert res.json() == {"error": "page not found"}


def test_head():
    with _client() as client:
        res = client.head("/qcli/docs/pages/index")
        assert res.status_code == 200 and res.content == b""
        assert client.head("/qcli/docs/pages").status_code == 200


def test_code_pages():
    with _client(lambda b: b.add_page("errors", "# Errors\nBody")) as client:
        pages = client.get("/qcli/docs/pages").json()
        assert {"slug": "errors", "title": "Errors", "group": "Guides", "order": 0} in pages
        assert client.get("/qcli/docs/pages/errors").json()["markdown"] == "# Errors\nBody"


def test_code_pages_without_a_folder():
    plugin = CliDocsBuilder().add_page("b", "text").add_page("a", "---\ntitle: Zed\n---\nx").build()
    assert [p.slug for p in plugin.pages] == ["b", "a"]
    result = create_cli_server()
    result.mount_plugin(plugin)
    with TestClient(result.app) as client:
        # order 0 for both, then title: "b" < "Zed" case-insensitively
        assert [p["slug"] for p in client.get("/qcli/docs/pages").json()] == ["b", "a"]


def test_page_order_ties():
    plugin = (
        CliDocsBuilder()
        .add_page("c", "# beta").add_page("a", "# Beta").add_page("b", "# alpha")
        .build()
    )
    result = create_cli_server()
    result.mount_plugin(plugin)
    with TestClient(result.app) as client:
        assert [p["slug"] for p in client.get("/qcli/docs/pages").json()] == ["b", "a", "c"]


def _bad_folder(tmp_path: Path, name: str, text: str = "x") -> str:
    folder = tmp_path / "bad"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return str(folder)


def test_startup_errors(tmp_path: Path):
    missing = str(tmp_path / "missing")
    cases: list[tuple[Callable[[CliDocsBuilder], Any], str]] = [
        (lambda b: b.add_page("Bad Slug", "x"), "invalid docs page slug: Bad Slug"),
        (lambda b: b.add_folder(_bad_folder(tmp_path, "Read Me.md")), "invalid docs page name: Read Me.md"),
        (lambda b: b.add_folder(str(FIXTURE)).add_page("index", "x"), "duplicate docs page: index"),
        (lambda b: b.add_folder(missing), f"docs folder not found: {missing}"),
        (lambda b: b.add_page("p", "---\norder: x\n---\nbody"), "invalid order in docs page: p"),
        (lambda b: b.set_variable("baseUrl", "x"), "docs variable is built in: baseUrl"),
        (lambda b: b.set_variable("name", "x"), "docs variable is built in: name"),
        (lambda b: b.set_variable("1x", "x"), "invalid docs variable: 1x"),
        (lambda b: b.set_base_path("/"), "invalid docs base path: /"),
        (lambda b: b.set_base_path("docs"), "invalid docs base path: docs"),
        (lambda b: b.set_base_path("/docs/"), "invalid docs base path: /docs/"),
        (lambda b: b.set_base_path("/docs?x"), "invalid docs base path: /docs?x"),
        (lambda b: b.set_base_path("/docs#x"), "invalid docs base path: /docs#x"),
        (lambda b: b.set_base_path("/a//b"), "invalid docs base path: /a//b"),
        (lambda b: b.set_base_path("/a b"), "invalid docs base path: /a b"),
    ]
    for configure, text in cases:
        builder = CliDocsBuilder()
        configure(builder)
        with pytest.raises(ValueError) as exc:
            builder.build()
        assert str(exc.value) == text


def test_duplicate_between_folders(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "Index.md").write_text("# Again", encoding="utf-8")
    with pytest.raises(ValueError, match="^duplicate docs page: index$"):
        CliDocsBuilder().add_folder(str(FIXTURE)).add_folder(str(other)).build()


def test_folder_rules(tmp_path: Path):
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "Setup.md").write_text("# Setup\r\nLine two\r\n", encoding="utf-8")
    (folder / "_draft.md").write_text("Bad Name Ignored", encoding="utf-8")
    (folder / ".hidden.md").write_text("x", encoding="utf-8")
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.md").write_text("x", encoding="utf-8")
    plugin = CliDocsBuilder().add_folder(str(folder)).build()
    assert [p.slug for p in plugin.pages] == ["setup"]
    assert plugin.pages[0] == DocsPage(slug="setup", title="Setup", group="Guides", order=0,
                                       markdown="# Setup\nLine two\n")


def test_folder_relative_to_the_working_directory(tmp_path: Path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("# Home", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert [p.slug for p in CliDocsBuilder().add_folder("docs").build().pages] == ["index"]


def test_front_matter_unclosed_is_body():
    page = parse_page("x", "---\ntitle: x\nbody")
    assert page.title == "x"
    assert page.markdown == "---\ntitle: x\nbody"


def test_front_matter_needs_the_exact_first_line():
    page = parse_page("p", " ---\ntitle: T\n---\nbody")
    assert page.title == "p"
    assert page.markdown == " ---\ntitle: T\n---\nbody"


def test_parse_quotes_and_unknown_keys():
    page = parse_page("p", "---\ntitle: 'A: B'\nauthor: x\ngroup: \"Basics\"\norder: -3\n---\n# H\nbody\n")
    assert (page.title, page.group, page.order) == ("A: B", "Basics", -3)
    assert page.markdown == "# H\nbody\n"
    mismatched = parse_page("p", "---\ntitle: 'A\"\n---\n")
    assert mismatched.title == "'A\""
    assert mismatched.markdown == ""


def test_title_variables_and_heading_default():
    page = parse_page("p", "intro\n# First heading\n# Second\n")
    assert page.title == "First heading"
    assert parse_page("p", "#NoSpace\n").title == "p"


def test_order_uses_the_title_as_written():
    # As written "{{name}}" sorts after "B"; replaced ("A") it would sort before.
    plugin = (
        CliDocsBuilder().set_name("A")
        .add_page("b", "---\ntitle: B\n---\n").add_page("n", "---\ntitle: \"{{name}}\"\n---\n")
        .build()
    )
    result = create_cli_server()
    result.mount_plugin(plugin)
    with TestClient(result.app) as client:
        pages = client.get("/qcli/docs/pages").json()
        assert [(p["slug"], p["title"]) for p in pages] == [("b", "B"), ("n", "A")]


def test_crlf_front_matter():
    page = parse_page("p", "---\r\ntitle: T\r\norder: 2\r\n---\r\nbody\r\n")
    assert (page.title, page.order, page.markdown) == ("T", 2, "body\n")


def test_ignores_credentials():
    with _client() as client:
        res = client.get("/qcli/docs/pages", headers={"Authorization": "Bearer garbage"})
        assert res.status_code == 200


def test_portal_section_and_host_routes():
    plugin = CliDocsBuilder().set_title("Docs").set_base_path("/developers/docs").build()
    assert plugin.portal_section == {"kind": "docs", "title": "Docs", "base_path": "/developers/docs"}
    assert plugin.cli_routes is False
    assert plugin.prefix == "/developers/docs"
    assert CliDocsBuilder().build().portal_section == {"kind": "docs", "title": "Guides", "base_path": "/qcli/docs"}


def test_custom_base_path():
    with _client(lambda b: b.set_base_path("/developers/docs")) as client:
        assert client.get("/developers/docs/pages").status_code == 200
        assert client.get("/qcli/docs/pages").status_code == 404


def test_other_paths_not_handled():
    with _client() as client:
        assert client.get("/qcli/docs").status_code == 404
        assert client.get("/qcli/docs/other").status_code == 404
        assert client.post("/qcli/docs/pages").status_code == 405


def test_base_url_follows_the_root_path():
    result = create_cli_server()
    result.mount_plugin(_builder().build())
    with TestClient(result.app, root_path="/svc") as client:
        markdown = client.get("/qcli/docs/pages/index").json()["markdown"]
        assert markdown.startswith("Call http://testserver/svc/v1/rates")


@pytest.mark.parametrize("root", ["//evil.example", "/\\evil.example"])
def test_invalid_root_path(root: str):
    result = create_cli_server()
    result.mount_plugin(_builder().build())
    with TestClient(result.app, root_path=root) as client:
        for path in ("/qcli/docs/pages", "/qcli/docs/pages/index"):
            res = client.get(path)
            assert (res.status_code, res.json()) == (400, {"error": "invalid path base"}), path


def test_symlinks_and_large_files_are_skipped(tmp_path: Path, caplog):
    import logging as _logging

    folder = tmp_path / "pages"
    folder.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("# Secret", encoding="utf-8")
    (folder / "linked.md").symlink_to(secret)
    (folder / "big.md").write_text("# Big\n" + "x" * (1024 * 1024), encoding="utf-8")
    (folder / "ok.md").write_text("# Ok", encoding="utf-8")
    with caplog.at_level(_logging.WARNING):
        plugin = CliDocsBuilder().add_folder(str(folder)).build()
    assert [p.slug for p in plugin.pages] == ["ok"]
    assert "a symbolic link" in caplog.text and "larger than 1 MB" in caplog.text


def test_json_content_type():
    with _client() as client:
        for path in ("/qcli/docs/pages", "/qcli/docs/pages/index", "/qcli/docs/pages/nope"):
            assert client.get(path).headers["content-type"] == "application/json; charset=utf-8", path
