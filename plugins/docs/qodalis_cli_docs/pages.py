"""Markdown pages: folders, front matter and variables (contract C9).

The front matter is parsed here, line by line (no YAML library): it is present
only when the first line is exactly ``---`` and ends at the next line that is
exactly ``---``; each line is ``key: value`` and only ``title``, ``group`` and
``order`` are read.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import Mapping

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VARIABLE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_.-]*)\s*\}\}")
ORDER_RE = re.compile(r"^[+-]?\d+$")
BUILT_IN_VARIABLES = ("baseUrl", "name")
DEFAULT_GROUP = "Guides"


@dataclass(frozen=True)
class DocsPage:
    slug: str
    title: str
    group: str
    order: int
    markdown: str

    def summary(self) -> dict[str, object]:
        return {"slug": self.slug, "title": self.title, "group": self.group, "order": self.order}

    def to_dict(self) -> dict[str, object]:
        return {**self.summary(), "markdown": self.markdown}


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_page(slug: str, text: str) -> DocsPage:
    """A page from its markdown: the front matter (``title``, ``group``,
    ``order``) removed, defaults applied (the first ``# `` heading, else the
    slug; ``Guides``; ``0``).  Raises ``ValueError`` for an ``order`` that
    is not an integer."""
    text = normalize_newlines(text)
    lines = text.split("\n")
    meta: dict[str, str] = {}
    body = text
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            end = -1
        if end > 0:
            for line in lines[1:end]:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                key = key.strip()
                if key in ("title", "group", "order"):
                    meta[key] = _unquote(value.strip())
            body = "\n".join(lines[end + 1:])

    order = 0
    if "order" in meta:
        if not ORDER_RE.match(meta["order"]):
            raise ValueError(f"invalid order in docs page: {slug}")
        order = int(meta["order"])
    title = meta.get("title")
    if title is None:
        heading = next((line[2:].strip() for line in body.split("\n") if line.startswith("# ")), None)
        title = heading or slug
    return DocsPage(slug=slug, title=title, group=meta.get("group", DEFAULT_GROUP), order=order, markdown=body)


def validate_slug(slug: object) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ValueError(f"invalid docs page slug: {slug}")
    return slug


def read_folder(path: str) -> list[DocsPage]:
    """Every ``*.md`` file directly in ``path`` (names starting with ``.`` or
    ``_`` ignored), in file-name order; ``path`` is relative to the working
    directory."""
    if not os.path.isdir(path):
        raise ValueError(f"docs folder not found: {path}")
    pages: list[DocsPage] = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if name.startswith((".", "_")) or not name.endswith(".md") or not os.path.isfile(full):
            continue
        slug = name[: -len(".md")].lower()
        if not SLUG_RE.match(slug):
            raise ValueError(f"invalid docs page name: {name}")
        with open(full, encoding="utf-8", newline="") as handle:
            pages.append(parse_page(slug, handle.read()))
    return pages


def validate_variable(key: object) -> str:
    if key in BUILT_IN_VARIABLES:
        raise ValueError(f"docs variable is built in: {key}")
    if not isinstance(key, str) or not VARIABLE_KEY_RE.match(key):
        raise ValueError(f"invalid docs variable: {key}")
    return key


def substitute(text: str, variables: Mapping[str, str]) -> str:
    """``{{ key }}`` replaced by its value; unknown keys left as written."""
    return VARIABLE_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), text)


def apply_variables(page: DocsPage, variables: Mapping[str, str]) -> DocsPage:
    return replace(page, title=substitute(page.title, variables), markdown=substitute(page.markdown, variables))


def page_order_key(page: DocsPage) -> tuple[int, str, str]:
    """``order``, then ``title`` (ordinal, case-insensitive), then ``slug``."""
    return (page.order, page.title.upper(), page.slug)


def sort_pages(pages: list[DocsPage]) -> list[DocsPage]:
    return sorted(pages, key=page_order_key)

