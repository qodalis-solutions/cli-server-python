"""Unit tests for CliResponseBuilder."""

from __future__ import annotations

from qodalis_cli.services import CliResponseBuilder


class TestCliResponseBuilder:
    def test_write_text_adds_text_output(self) -> None:
        b = CliResponseBuilder()
        b.write_text("hello")
        resp = b.build()
        assert len(resp.outputs) == 1
        out = resp.outputs[0]
        assert out.type == "text"  # type: ignore[union-attr]
        assert out.value == "hello"  # type: ignore[union-attr]

    def test_write_text_with_style(self) -> None:
        b = CliResponseBuilder()
        b.write_text("err", style="error")
        resp = b.build()
        assert resp.outputs[0].style == "error"  # type: ignore[union-attr]

    def test_write_table_adds_table_output(self) -> None:
        b = CliResponseBuilder()
        b.write_table(["Col1", "Col2"], [["a", "b"]])
        resp = b.build()
        out = resp.outputs[0]
        assert out.type == "table"  # type: ignore[union-attr]
        assert out.headers == ["Col1", "Col2"]  # type: ignore[union-attr]
        assert out.rows == [["a", "b"]]  # type: ignore[union-attr]

    def test_write_list_adds_list_output(self) -> None:
        b = CliResponseBuilder()
        b.write_list(["x", "y", "z"])
        resp = b.build()
        out = resp.outputs[0]
        assert out.type == "list"  # type: ignore[union-attr]
        assert out.items == ["x", "y", "z"]  # type: ignore[union-attr]

    def test_write_list_ordered(self) -> None:
        b = CliResponseBuilder()
        b.write_list(["a"], ordered=True)
        resp = b.build()
        assert resp.outputs[0].ordered is True  # type: ignore[union-attr]

    def test_write_json_adds_json_output(self) -> None:
        b = CliResponseBuilder()
        b.write_json({"key": "val"})
        resp = b.build()
        out = resp.outputs[0]
        assert out.type == "json"  # type: ignore[union-attr]
        assert out.value == {"key": "val"}  # type: ignore[union-attr]

    def test_write_key_value_adds_key_value_output(self) -> None:
        b = CliResponseBuilder()
        b.write_key_value({"name": "alice", "age": "30"})
        resp = b.build()
        out = resp.outputs[0]
        assert out.type == "key-value"  # type: ignore[union-attr]
        entries = out.entries  # type: ignore[union-attr]
        assert len(entries) == 2
        assert entries[0].key == "name"
        assert entries[0].value == "alice"
        assert entries[1].key == "age"
        assert entries[1].value == "30"

    def test_set_exit_code(self) -> None:
        b = CliResponseBuilder()
        b.set_exit_code(42)
        resp = b.build()
        assert resp.exit_code == 42

    def test_default_exit_code_is_zero(self) -> None:
        b = CliResponseBuilder()
        resp = b.build()
        assert resp.exit_code == 0

    def test_build_returns_correct_shape(self) -> None:
        b = CliResponseBuilder()
        b.write_text("a")
        b.write_json(1)
        b.set_exit_code(5)
        resp = b.build()
        assert resp.exit_code == 5
        assert len(resp.outputs) == 2
        data = resp.model_dump(by_alias=True, exclude_none=True)
        assert "exitCode" in data
        assert "outputs" in data
