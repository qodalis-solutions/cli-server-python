"""``docs``: the documentation pages in the terminal."""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Mapping

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliModule,
    CliProcessCommand,
    ICliCommandProcessor,
    get_positionals,
)

from .pages import DocsPage, apply_variables

Variables = Callable[[], Mapping[str, str]]

_JSON = CliCommandParameterDescriptor("json", "Print the result as JSON", False, "boolean")
_GROUP = CliCommandParameterDescriptor("group", "Only the pages of this group", False, "string")


def _flag(command: CliProcessCommand, name: str) -> bool:
    value = (command.args or {}).get(name)
    return value is True or (isinstance(value, str) and value.lower() in ("true", "1", ""))


class DocsListProcessor(CliCommandProcessor):
    command = "list"
    aliases = ["ls"]
    description = "Lists the documentation pages by group: slug and title"
    parameters = [_GROUP, _JSON]
    examples = [
        CliCommandExample("docs list", "Every page"),
        CliCommandExample("docs list --group Guides --json", "The pages of one group as JSON"),
    ]

    def __init__(self, pages: list[DocsPage], variables: Variables) -> None:
        self._pages = pages
        self._variables = variables

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        group = (command.args or {}).get("group")
        if group is True or (group is not None and str(group).strip() == ""):
            return "Error: --group needs a group name"
        variables = self._variables()
        items = [apply_variables(p, variables).summary() for p in self._pages]
        if group is not None:
            items = [p for p in items if str(p["group"]).lower() == str(group).lower()]
        if _flag(command, "json"):
            return json.dumps(items, indent=2, ensure_ascii=False)
        if not items:
            return f"No documentation pages in the group {group}." if group is not None else "No documentation pages."
        width = max(len(str(p["slug"])) for p in items)
        # Grouped in the order each group first appears (the page order)
        groups: dict[str, list[dict]] = {}
        for page in items:
            groups.setdefault(str(page["group"]), []).append(page)
        lines: list[str] = []
        for name, pages in groups.items():
            if lines:
                lines.append("")
            lines.append(name)
            lines.extend(f"  {str(p['slug']).ljust(width)}  {p['title']}" for p in pages)
        return "\n".join(lines)


class DocsReadProcessor(CliCommandProcessor):
    command = "read"
    aliases = ["show", "cat"]
    description = "Prints a documentation page as markdown, variables replaced"
    value_required = False
    parameters = [_JSON]
    examples = [CliCommandExample("docs read getting-started", "The page getting-started")]

    def __init__(self, by_slug: Mapping[str, DocsPage], variables: Variables) -> None:
        self._by_slug = by_slug
        self._variables = variables

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        words = get_positionals(command)
        if not words:
            return "Error: name the page: docs read <slug> (docs list shows them)"
        if len(words) > 1:
            return "Error: one page at a time: docs read <slug>"
        page = self._by_slug.get(words[0])
        if page is None:
            return f"Error: page not found: {words[0]} (docs list shows the pages)"
        view = apply_variables(page, self._variables())
        if _flag(command, "json"):
            return json.dumps(view.to_dict(), indent=2, ensure_ascii=False)
        markdown = view.markdown.strip()
        # The title lives in the front matter; shown as a heading unless the page starts with its own
        if markdown.startswith("#"):
            return markdown
        return f"# {view.title}" + (f"\n\n{markdown}" if markdown else "")


class CliDocsCommandProcessor(CliCommandProcessor):
    """``docs`` (= ``docs list``), ``docs list [--group <g>] [--json]``,
    ``docs read <slug> [--json]``: public like the pages."""

    command = "docs"
    description = "Documentation pages: list them, read one as markdown"
    module = "docs"
    icon = "📖"

    def __init__(self, pages: list[DocsPage], variables: Variables) -> None:
        self._list = DocsListProcessor(pages, variables)
        self._processors: list[ICliCommandProcessor] = [
            self._list, DocsReadProcessor({p.slug: p for p in pages}, variables),
        ]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        return await self._list.handle_async(command, cancellation_event)


class DocsModule(CliModule):
    """The ``docs`` terminal command, registered by ``mount_plugin``."""

    name = "docs"
    description = "Documentation pages in the terminal"
    icon = "📖"

    def __init__(self, processor: CliDocsCommandProcessor, version: str) -> None:
        self._processor = processor
        self._version = version

    @property
    def version(self) -> str:  # type: ignore[override]
        return self._version

    @property
    def processors(self) -> list[ICliCommandProcessor]:  # type: ignore[override]
        return [self._processor]
