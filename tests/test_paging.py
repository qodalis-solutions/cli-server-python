"""Paged tables (``--skip`` / ``--take``) and the row cap for unpaged tables."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import (
    DEFAULT_TAKE,
    MAX_TAKE,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliPageRequest,
    CliPaging,
    CliProcessCommand,
    CliResponseBuilder,
    CliServerOptions,
    CliServerResponse,
    create_cli_server,
)
from qodalis_cli.mcp import command_tool_name
from qodalis_cli.mcp.commands import describe_page, render_outputs
from qodalis_cli.models.cli_server_output import CliServerTableOutput, CliServerTablePage, CliServerTextOutput
from qodalis_cli.services import apply_output_limits, apply_response_limits
from qodalis_cli_server_abstractions import ICliStreamCommandProcessor

BASE = "/api/v1/qcli"
MCP = "/api/qcli/mcp"


def _cmd(**args: Any) -> CliProcessCommand:
    return CliProcessCommand(command="list", args=args)


def _rows(n: int) -> list[list[str]]:
    return [[str(i)] for i in range(n)]


# ---------------------------------------------------------------------------
# CliPaging
# ---------------------------------------------------------------------------


class TestCliPaging:
    def test_defaults(self) -> None:
        page = CliPaging.from_command(_cmd())
        assert page == CliPageRequest(skip=0, take=50)
        assert DEFAULT_TAKE == 50 and MAX_TAKE == 500
        assert CliPaging.DEFAULT_TAKE == 50 and CliPaging.MAX_TAKE == 500

    def test_reads_ints_and_strings(self) -> None:
        assert CliPaging.from_command(_cmd(skip=40, take=20)) == CliPageRequest(40, 20)
        assert CliPaging.from_command(_cmd(skip="40", take="20")) == CliPageRequest(40, 20)
        assert CliPaging.from_command(_cmd(skip=0, take=1)) == CliPageRequest(0, 1)

    def test_caps_take(self) -> None:
        assert CliPaging.from_command(_cmd(take=10000)).take == MAX_TAKE
        assert CliPaging.from_command(_cmd(take=500)).take == 500

    def test_negative_skip_becomes_zero(self) -> None:
        assert CliPaging.from_command(_cmd(skip=-5)).skip == 0
        assert CliPaging.from_command(_cmd(skip="-5")).skip == 0

    @pytest.mark.parametrize("bad", [0, -2, "abc", "", None, "2.5", 2.5, True, False])
    def test_invalid_values_fall_back(self, bad: Any) -> None:
        assert CliPaging.from_command(_cmd(take=bad)).take == DEFAULT_TAKE
        assert CliPaging.from_command(_cmd(skip=bad)).skip == 0

    def test_custom_default_take(self) -> None:
        assert CliPaging.from_command(_cmd(), default_take=25).take == 25
        assert CliPaging.from_command(_cmd(take=0), default_take=25).take == 25

    def test_no_page_aliases(self) -> None:
        assert CliPaging.from_command(_cmd(page=3, **{"page-size": 20})) == CliPageRequest(0, 50)

    def test_parameters(self) -> None:
        params = CliPaging.parameters()
        assert [p.name for p in params] == ["skip", "take"]
        assert all(p.type == "number" for p in params)
        assert [p.default_value for p in params] == [0, 50]
        assert params[0].description == "Rows to skip"
        assert params[1].description == "Rows to show (at most 500)"
        assert CliPaging.parameters(default_take=25)[1].default_value == 25


# ---------------------------------------------------------------------------
# write_paged_table and the wire format
# ---------------------------------------------------------------------------


class TestWritePagedTable:
    def _page(self, **kw: Any) -> dict[str, Any]:
        builder = CliResponseBuilder()
        builder.write_paged_table(["N"], kw.pop("rows"), **kw)
        return builder.build().outputs[0].page.model_dump(by_alias=True, exclude_none=True)

    def test_has_more_from_total(self) -> None:
        assert self._page(rows=_rows(50), page=CliPageRequest(50, 50), total=120)["hasMore"] is True
        assert self._page(rows=_rows(20), page=CliPageRequest(100, 50), total=120)["hasMore"] is False

    def test_has_more_without_total_defaults_to_false(self) -> None:
        assert self._page(rows=_rows(50), page=CliPageRequest(0, 50)) == {
            "skip": 0, "take": 50, "hasMore": False,
        }

    def test_explicit_has_more_wins(self) -> None:
        assert self._page(rows=_rows(50), page=CliPageRequest(0, 50), has_more=True)["hasMore"] is True

    def test_json_shape(self) -> None:
        builder = CliResponseBuilder()
        builder.write_paged_table(["N"], [["a"]], CliPageRequest(50, 50), total=120, has_more=True)
        builder.write_table(["N"], [["b"]])
        data = builder.build().model_dump(by_alias=True, exclude_none=True)
        assert data["outputs"][0] == {
            "type": "table",
            "headers": ["N"],
            "rows": [["a"]],
            "page": {"skip": 50, "take": 50, "total": 120, "hasMore": True},
        }
        # No page on a plain table, and nothing null-valued anywhere.
        assert "page" not in data["outputs"][1]
        assert "null" not in json.dumps(data)


# ---------------------------------------------------------------------------
# Row cap
# ---------------------------------------------------------------------------


class TestOutputLimits:
    def test_cuts_unpaged_table(self) -> None:
        out = apply_output_limits(CliServerTableOutput(headers=["N"], rows=_rows(300)), 200)
        assert len(out.rows) == 200 and out.rows[-1] == ["199"]
        assert out.page.model_dump(by_alias=True, exclude_none=True) == {
            "skip": 0, "take": 200, "total": 300, "hasMore": True, "truncated": True,
        }

    def test_leaves_small_and_paged_tables(self) -> None:
        small = CliServerTableOutput(headers=["N"], rows=_rows(200))
        assert apply_output_limits(small, 200) is small
        paged = CliServerTableOutput(
            headers=["N"], rows=_rows(300), page=CliServerTablePage(skip=0, take=300, has_more=False),
        )
        assert apply_output_limits(paged, 200) is paged

    def test_zero_disables(self) -> None:
        table = CliServerTableOutput(headers=["N"], rows=_rows(300))
        assert apply_output_limits(table, 0) is table

    def test_dict_outputs(self) -> None:
        out = apply_output_limits({"type": "table", "headers": ["N"], "rows": _rows(300)}, 200)
        assert len(out["rows"]) == 200
        assert out["page"] == {"skip": 0, "take": 200, "total": 300, "hasMore": True, "truncated": True}
        text = {"type": "text", "value": "x"}
        assert apply_output_limits(text, 200) is text

    def test_response(self) -> None:
        response = CliServerResponse(exitCode=0, outputs=[
            CliServerTextOutput(value="x"), CliServerTableOutput(headers=["N"], rows=_rows(300)),
        ])
        outputs = apply_response_limits(response, 200).outputs
        assert outputs[0].value == "x" and len(outputs[1].rows) == 200


# ---------------------------------------------------------------------------
# Server wiring: execute, execute/stream, MCP
# ---------------------------------------------------------------------------


class BigTableProcessor(CliCommandProcessor):
    """Returns ``--count`` rows, unpaged, or a page of them with ``--paged``."""

    @property
    def command(self) -> str:
        return "big"

    @property
    def description(self) -> str:
        return "A long table"

    @property
    def parameters(self):
        return [
            *CliPaging.parameters(),
            CliCommandParameterDescriptor(name="count", description="Rows", type="number"),
            CliCommandParameterDescriptor(name="paged", description="Page the rows", type="boolean"),
        ]

    async def handle_async(self, command, cancellation_event=None) -> str:
        return ""

    async def handle_structured_async(self, command, cancellation_event=None) -> CliServerResponse:
        count = int(command.args.get("count", 300))
        rows = _rows(count)
        builder = CliResponseBuilder()
        if command.args.get("paged"):
            page = CliPaging.from_command(command)
            builder.write_paged_table(["N"], rows[page.skip:page.skip + page.take], page, total=count)
        else:
            builder.write_table(["N"], rows)
        return builder.build()


class BigStreamProcessor(CliCommandProcessor, ICliStreamCommandProcessor):
    @property
    def command(self) -> str:
        return "big-stream"

    @property
    def description(self) -> str:
        return "A long streamed table"

    async def handle_async(self, command, cancellation_event=None) -> str:
        return ""

    async def handle_stream_async(self, command, emit, cancellation_event: asyncio.Event | None = None) -> int:
        emit({"type": "table", "headers": ["N"], "rows": _rows(300)})
        emit({"type": "text", "value": "done"})
        return 0


def _client(**opts: Any) -> TestClient:
    def configure(builder) -> None:
        builder.add_processor(BigTableProcessor())
        builder.add_processor(BigStreamProcessor())

    return TestClient(create_cli_server(CliServerOptions(configure=configure, mcp=True, **opts)).app)


def _sse_outputs(text: str) -> list[dict[str, Any]]:
    outputs = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        if lines[0] == "event: output":
            outputs.append(json.loads(lines[1][len("data: "):]))
    return outputs


class TestServerWiring:
    def test_execute_caps_unpaged_table(self) -> None:
        out = _client().post(f"{BASE}/execute", json={"command": "big"}).json()["outputs"][0]
        assert len(out["rows"]) == 200
        assert out["page"] == {"skip": 0, "take": 200, "total": 300, "hasMore": True, "truncated": True}

    def test_execute_keeps_paged_table(self) -> None:
        body = {"command": "big", "args": {"paged": True, "skip": 50, "take": 50, "count": 120}}
        out = _client().post(f"{BASE}/execute", json=body).json()["outputs"][0]
        assert out["rows"][0] == ["50"] and len(out["rows"]) == 50
        assert out["page"] == {"skip": 50, "take": 50, "total": 120, "hasMore": True}

    def test_execute_small_table_has_no_page(self) -> None:
        out = _client().post(f"{BASE}/execute", json={"command": "big", "args": {"count": 5}}).json()["outputs"][0]
        assert "page" not in out and len(out["rows"]) == 5

    def test_custom_and_disabled_cap(self) -> None:
        out = _client(max_table_rows=100).post(f"{BASE}/execute", json={"command": "big"}).json()["outputs"][0]
        assert len(out["rows"]) == 100 and out["page"]["take"] == 100
        out = _client(max_table_rows=0).post(f"{BASE}/execute", json={"command": "big"}).json()["outputs"][0]
        assert len(out["rows"]) == 300 and "page" not in out

    def test_stream_caps_executor_outputs(self) -> None:
        res = _client().post(f"{BASE}/execute/stream", json={"command": "big"})
        (out,) = _sse_outputs(res.text)
        assert len(out["rows"]) == 200 and out["page"]["truncated"] is True

    def test_stream_caps_emitted_outputs(self) -> None:
        res = _client().post(f"{BASE}/execute/stream", json={"command": "big-stream"})
        table, text = _sse_outputs(res.text)
        assert len(table["rows"]) == 200
        assert table["page"] == {"skip": 0, "take": 200, "total": 300, "hasMore": True, "truncated": True}
        assert text == {"type": "text", "value": "done"}

    def test_mcp_describes_pages(self) -> None:
        client = _client()

        def call(arguments: dict[str, Any]) -> str:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": command_tool_name(["big"]), "arguments": arguments}}
            return client.post(MCP, json=body).json()["result"]["content"][0]["text"]

        text = call({})
        assert text.splitlines()[-1] == (
            "(first 200 of 300 rows; the command does not page, narrow it down to see the rest)"
        )
        assert len(text.splitlines()) == 1 + 200 + 1
        text = call({"paged": True, "skip": 50, "take": 50, "count": 120})
        assert text.splitlines()[-1] == "(rows 51-100 of 120; next: skip=100)"
        text = call({"paged": True, "skip": 100, "take": 50, "count": 120})
        assert text.splitlines()[-1] == "(rows 101-120 of 120)"


class TestDescribePage:
    def test_lines(self) -> None:
        assert describe_page({"skip": 50, "take": 50, "total": 120, "hasMore": True}, 50) == (
            "(rows 51-100 of 120; next: skip=100)"
        )
        assert describe_page({"skip": 50, "take": 50, "hasMore": True}, 50) == "(rows 51-100; next: skip=100)"
        assert describe_page({"skip": 100, "take": 50, "total": 120, "hasMore": False}, 20) == (
            "(rows 101-120 of 120)"
        )
        assert describe_page({"skip": 200, "take": 50, "total": 120, "hasMore": False}, 0) == (
            "(no rows at skip=200 of 120)"
        )
        assert describe_page({"skip": 200, "take": 50, "hasMore": False}, 0) == "(no rows at skip=200)"
        assert describe_page({"skip": 0, "take": 200, "total": 300, "hasMore": True, "truncated": True}, 200) == (
            "(first 200 of 300 rows; the command does not page, narrow it down to see the rest)"
        )

    def test_render_outputs(self) -> None:
        text = render_outputs([
            {"type": "table", "headers": ["A"], "rows": [["1"]], "page": {"skip": 100, "take": 50, "hasMore": False}},
            {"type": "table", "headers": ["B"], "rows": [["2"]]},
        ])
        assert text == "A\n1\n(rows 101-101)\nB\n2"
