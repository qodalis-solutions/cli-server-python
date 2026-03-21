"""Tests for the 5 new data explorer providers.

All five providers depend on third-party database drivers (redis, elasticsearch,
pymssql, asyncpg, aiomysql) that are not installed in the test environment.
We mock those modules at the sys.modules level before importing the providers,
so we can test everything that does NOT require a live database connection:
  - instantiation and attribute assignment
  - language / interface contract
  - module-level parsing utilities (_parse_connection_string, _normalise_result,
    _decode)
  - MySQL URL parsing via urlparse (done inside __init__)
  - Elasticsearch query parser (inline in execute_async, extracted via a helper
    that we call directly on the class)
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Mock out all third-party driver dependencies before any provider is imported.
# This block must run before any plugin import so that Python never tries to
# actually load the real C extensions / pure-Python drivers.
# ---------------------------------------------------------------------------

_MOCK_MODULES = [
    "redis",
    "redis.asyncio",
    "elasticsearch",
    "pymssql",
    "asyncpg",
    "aiomysql",
]
for _mod in _MOCK_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# ---------------------------------------------------------------------------
# Add plugin source trees to sys.path so the packages are importable without
# being pip-installed.
# ---------------------------------------------------------------------------

import os as _os

_PLUGINS_ROOT = _os.path.join(_os.path.dirname(__file__), "..", "plugins")

for _plugin in (
    "data-explorer-redis",
    "data-explorer-elasticsearch",
    "data-explorer-mssql",
    "data-explorer-postgres",
    "data-explorer-mysql",
):
    _plugin_path = _os.path.join(_PLUGINS_ROOT, _plugin)
    if _plugin_path not in sys.path:
        sys.path.insert(0, _plugin_path)

# ---------------------------------------------------------------------------
# Now it is safe to import the providers and helpers.
# ---------------------------------------------------------------------------

from qodalis_cli_data_explorer_redis import RedisDataExplorerProvider  # noqa: E402
from qodalis_cli_data_explorer_redis.redis_provider import (  # noqa: E402
    _ALLOWED_COMMANDS,
    _LIST_COMMANDS,
    _PAIR_LIST_COMMANDS,
    _SCALAR_COMMANDS,
    _decode,
    _normalise_result,
)
from qodalis_cli_data_explorer_elasticsearch import (  # noqa: E402
    ElasticsearchDataExplorerProvider,
)
from qodalis_cli_data_explorer_mssql import MssqlDataExplorerProvider  # noqa: E402
from qodalis_cli_data_explorer_mssql.mssql_provider import (  # noqa: E402
    _parse_connection_string,
)
from qodalis_cli_data_explorer_postgres import PostgresDataExplorerProvider  # noqa: E402
from qodalis_cli_data_explorer_mysql import MysqlDataExplorerProvider  # noqa: E402

from qodalis_cli_server_abstractions import (  # noqa: E402
    DataExplorerLanguage,
    DataExplorerProviderOptions,
)


# ===========================================================================
# Helpers / fixtures
# ===========================================================================


def _default_options(
    name: str = "test",
    language: DataExplorerLanguage = DataExplorerLanguage.SQL,
) -> DataExplorerProviderOptions:
    return DataExplorerProviderOptions(name=name, description="test", language=language)


# ===========================================================================
# PostgreSQL provider
# ===========================================================================


class TestPostgresProvider:
    def test_instantiation(self) -> None:
        provider = PostgresDataExplorerProvider(
            "postgresql://user:pass@localhost:5432/mydb"
        )
        assert provider is not None

    def test_stores_connection_string(self) -> None:
        cs = "postgresql://user:pass@localhost:5432/mydb"
        provider = PostgresDataExplorerProvider(cs)
        assert provider._connection_string == cs

    def test_has_expected_interface_methods(self) -> None:
        provider = PostgresDataExplorerProvider(
            "postgresql://user:pass@localhost:5432/mydb"
        )
        assert callable(getattr(provider, "execute_async", None))
        assert callable(getattr(provider, "get_schema_async", None))

    def test_instantiation_minimal_connection_string(self) -> None:
        # Should not raise even with a minimal string
        provider = PostgresDataExplorerProvider("postgresql://localhost/testdb")
        assert provider is not None


# ===========================================================================
# MySQL provider
# ===========================================================================


class TestMysqlProvider:
    def test_instantiation(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:pass@localhost:3306/mydb")
        assert provider is not None

    def test_url_parsing_host(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:pass@dbhost:3306/mydb")
        assert provider._host == "dbhost"

    def test_url_parsing_port(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:pass@localhost:3307/mydb")
        assert provider._port == 3307

    def test_url_parsing_default_port(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:pass@localhost/mydb")
        assert provider._port == 3306

    def test_url_parsing_user(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://dbuser:pass@localhost:3306/mydb")
        assert provider._user == "dbuser"

    def test_url_parsing_password(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:s3cr3t@localhost:3306/mydb")
        assert provider._password == "s3cr3t"

    def test_url_parsing_database(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:pass@localhost:3306/myschema")
        assert provider._db == "myschema"

    def test_url_parsing_default_user(self) -> None:
        # Missing user in URL → should default to "root"
        provider = MysqlDataExplorerProvider("mysql://localhost/mydb")
        assert provider._user == "root"

    def test_url_parsing_default_host(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root@/mydb")
        # urlparse gives None for hostname when only "root@" is present
        # provider falls back to "localhost"
        assert provider._host == "localhost"

    def test_has_expected_interface_methods(self) -> None:
        provider = MysqlDataExplorerProvider("mysql://root:pass@localhost:3306/mydb")
        assert callable(getattr(provider, "execute_async", None))
        assert callable(getattr(provider, "get_schema_async", None))


# ===========================================================================
# MSSQL provider
# ===========================================================================


class _MSSQL_CS:
    FULL = "Server=myhost,1433;Database=mydb;User Id=myuser;Password=mypass;TrustServerCertificate=true"
    NO_PORT = "Server=myhost;Database=mydb;User Id=myuser;Password=mypass"
    UID_ALIAS = "Server=myhost,1433;Database=mydb;Uid=myuser;Pwd=mypass"


class TestMssqlProvider:
    def test_instantiation(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert provider is not None

    def test_host_extracted(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert provider._host == "myhost"

    def test_port_extracted(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert provider._port == 1433

    def test_port_defaults_to_1433_when_absent(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.NO_PORT)
        assert provider._port == 1433

    def test_database_extracted(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert provider._database == "mydb"

    def test_user_extracted(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert provider._user == "myuser"

    def test_password_extracted(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert provider._password == "mypass"

    def test_has_expected_interface_methods(self) -> None:
        provider = MssqlDataExplorerProvider(_MSSQL_CS.FULL)
        assert callable(getattr(provider, "execute_async", None))
        assert callable(getattr(provider, "get_schema_async", None))


class TestParseConnectionString:
    """Unit-tests for the module-level _parse_connection_string helper."""

    def test_basic_keys_are_lowercased(self) -> None:
        result = _parse_connection_string("Server=host;Database=db")
        assert "server" in result
        assert "database" in result

    def test_spaces_in_key_are_removed(self) -> None:
        # "User Id" → "userid"
        result = _parse_connection_string("User Id=sa")
        assert "userid" in result
        assert result["userid"] == "sa"

    def test_server_value(self) -> None:
        result = _parse_connection_string("Server=myhost,1433;Database=mydb")
        assert result["server"] == "myhost,1433"

    def test_password_with_special_characters(self) -> None:
        result = _parse_connection_string("Password=P@ss!word")
        assert result["password"] == "P@ss!word"

    def test_trust_server_certificate_key(self) -> None:
        result = _parse_connection_string(
            "Server=host;TrustServerCertificate=true"
        )
        assert result["trustservercertificate"] == "true"

    def test_empty_string_returns_empty_dict(self) -> None:
        assert _parse_connection_string("") == {}

    def test_single_key_value(self) -> None:
        result = _parse_connection_string("Database=onlydb")
        assert result == {"database": "onlydb"}

    def test_values_stripped_of_surrounding_whitespace(self) -> None:
        result = _parse_connection_string("Server= myhost ; Database= mydb ")
        assert result["server"] == "myhost"
        assert result["database"] == "mydb"


# ===========================================================================
# Redis provider
# ===========================================================================


class TestRedisProvider:
    def test_instantiation_default(self) -> None:
        provider = RedisDataExplorerProvider()
        assert provider is not None

    def test_instantiation_custom_url(self) -> None:
        provider = RedisDataExplorerProvider("redis://myhost:6380/1")
        assert provider._connection_string == "redis://myhost:6380/1"

    def test_has_expected_interface_methods(self) -> None:
        provider = RedisDataExplorerProvider()
        assert callable(getattr(provider, "execute_async", None))
        assert callable(getattr(provider, "get_schema_async", None))


class TestAllowedCommands:
    def test_get_is_allowed(self) -> None:
        assert "GET" in _ALLOWED_COMMANDS

    def test_set_is_allowed(self) -> None:
        assert "SET" in _ALLOWED_COMMANDS

    def test_dangerous_commands_not_present(self) -> None:
        # FLUSHALL and FLUSHDB are destructive and should not be in the allow-list
        assert "FLUSHALL" not in _ALLOWED_COMMANDS
        assert "FLUSHDB" not in _ALLOWED_COMMANDS
        assert "CONFIG" not in _ALLOWED_COMMANDS
        assert "SHUTDOWN" not in _ALLOWED_COMMANDS
        assert "SLAVEOF" not in _ALLOWED_COMMANDS

    def test_read_commands_are_allowed(self) -> None:
        for cmd in ("HGETALL", "KEYS", "SMEMBERS", "LRANGE", "ZRANGE", "INFO", "PING"):
            assert cmd in _ALLOWED_COMMANDS, f"Expected {cmd} to be allowed"


class TestDecode:
    def test_bytes_to_str(self) -> None:
        assert _decode(b"hello") == "hello"

    def test_str_unchanged(self) -> None:
        assert _decode("hello") == "hello"

    def test_int_unchanged(self) -> None:
        assert _decode(42) == 42

    def test_list_of_bytes(self) -> None:
        assert _decode([b"a", b"b"]) == ["a", "b"]

    def test_nested_list(self) -> None:
        assert _decode([[b"a"], [b"b"]]) == [["a"], ["b"]]

    def test_dict_with_bytes_keys_and_values(self) -> None:
        assert _decode({b"key": b"value"}) == {"key": "value"}

    def test_none_unchanged(self) -> None:
        assert _decode(None) is None

    def test_invalid_utf8_replaced(self) -> None:
        result = _decode(b"\xff\xfe")
        assert isinstance(result, str)


class TestNormaliseResult:
    """Unit-tests for _normalise_result — pure logic, no Redis connection."""

    # --- HGETALL (pair list) ---

    def test_hgetall_dict_form(self) -> None:
        cols, rows = _normalise_result("HGETALL", {"field1": "v1", "field2": "v2"})
        assert cols == ["field", "value"]
        assert ["field1", "v1"] in rows
        assert ["field2", "v2"] in rows

    def test_hgetall_alternating_list_form(self) -> None:
        cols, rows = _normalise_result("HGETALL", ["f1", "v1", "f2", "v2"])
        assert cols == ["field", "value"]
        assert rows == [["f1", "v1"], ["f2", "v2"]]

    def test_hgetall_scalar_fallback(self) -> None:
        # If somehow a non-dict, non-list value comes back
        cols, rows = _normalise_result("HGETALL", "unexpected")
        assert cols == ["value"]
        assert rows == [["unexpected"]]

    # --- LIST commands ---

    def test_keys_returns_single_column(self) -> None:
        cols, rows = _normalise_result("KEYS", ["key1", "key2", "key3"])
        assert cols == ["value"]
        assert rows == [["key1"], ["key2"], ["key3"]]

    def test_smembers_returns_single_column(self) -> None:
        cols, rows = _normalise_result("SMEMBERS", ["member1", "member2"])
        assert cols == ["value"]
        assert len(rows) == 2

    def test_lrange_returns_single_column(self) -> None:
        cols, rows = _normalise_result("LRANGE", ["a", "b", "c"])
        assert cols == ["value"]
        assert rows == [["a"], ["b"], ["c"]]

    def test_list_command_with_scalar_value(self) -> None:
        # Edge case: a list command receiving a scalar
        cols, rows = _normalise_result("KEYS", "single_value")
        assert cols == ["value"]
        assert rows == [["single_value"]]

    # --- SCALAR commands ---

    def test_get_returns_single_row(self) -> None:
        cols, rows = _normalise_result("GET", "myvalue")
        assert cols == ["value"]
        assert rows == [["myvalue"]]

    def test_dbsize_returns_integer(self) -> None:
        cols, rows = _normalise_result("DBSIZE", 42)
        assert cols == ["value"]
        assert rows == [[42]]

    def test_ping_decodes_bytes(self) -> None:
        # bytes should be decoded by _decode before normalisation
        cols, rows = _normalise_result("PING", b"PONG")
        assert cols == ["value"]
        assert rows == [["PONG"]]

    def test_exists_returns_integer(self) -> None:
        cols, rows = _normalise_result("EXISTS", 1)
        assert rows == [[1]]

    # --- Generic fallback ---

    def test_unknown_command_str_fallback(self) -> None:
        cols, rows = _normalise_result("SOMECOMMAND", "rawval")
        assert cols == ["value"]
        assert rows == [["rawval"]]

    def test_unknown_command_list_fallback(self) -> None:
        cols, rows = _normalise_result("SOMECOMMAND", ["x", "y"])
        assert cols == ["value"]
        assert rows == [["x"], ["y"]]

    def test_unknown_command_dict_fallback(self) -> None:
        cols, rows = _normalise_result("SOMECOMMAND", {"a": 1, "b": 2})
        assert set(cols) == {"a", "b"}
        assert len(rows) == 1


# ===========================================================================
# Elasticsearch provider
# ===========================================================================


class TestElasticsearchProvider:
    def test_instantiation_default(self) -> None:
        provider = ElasticsearchDataExplorerProvider()
        assert provider is not None

    def test_instantiation_custom_node(self) -> None:
        provider = ElasticsearchDataExplorerProvider("http://eshost:9200")
        assert provider._node == "http://eshost:9200"

    def test_has_expected_interface_methods(self) -> None:
        provider = ElasticsearchDataExplorerProvider()
        assert callable(getattr(provider, "execute_async", None))
        assert callable(getattr(provider, "get_schema_async", None))


class TestElasticsearchQueryParser:
    """
    The Elasticsearch provider parses queries inline in execute_async.
    We test the parsing logic by extracting the same rules the provider uses
    and running them directly without making any network calls.
    """

    @staticmethod
    def _parse_query(query: str) -> tuple[str, str, Any]:
        """
        Replicate the provider's inline query-parsing logic and return
        (method, path, body).
        """
        import json

        lines = query.strip().splitlines()
        first_line = lines[0].strip() if lines else ""
        parts = first_line.split(None, 1)

        if len(parts) == 2 and parts[0].upper() in {
            "GET", "POST", "PUT", "DELETE", "HEAD", "PATCH",
        }:
            method = parts[0].upper()
            path = parts[1].strip()
        else:
            method = "GET"
            path = first_line

        body_lines = lines[1:]
        body_text = "\n".join(body_lines).strip()
        body = json.loads(body_text) if body_text else None

        # Append ?format=json for _cat endpoints
        if "/_cat/" in path or path.startswith("_cat/"):
            if "format=json" not in path:
                sep = "&" if "?" in path else "?"
                path = f"{path}{sep}format=json"

        return method, path, body

    def test_get_with_explicit_verb(self) -> None:
        method, path, body = self._parse_query("GET /myindex/_search")
        assert method == "GET"
        assert path == "/myindex/_search"
        assert body is None

    def test_post_with_json_body(self) -> None:
        query = 'POST /myindex/_search\n{"query": {"match_all": {}}}'
        method, path, body = self._parse_query(query)
        assert method == "POST"
        assert path == "/myindex/_search"
        assert body == {"query": {"match_all": {}}}

    def test_implicit_get_when_no_verb(self) -> None:
        method, path, body = self._parse_query("/myindex/_search")
        assert method == "GET"
        assert path == "/myindex/_search"

    def test_delete_verb(self) -> None:
        method, path, body = self._parse_query("DELETE /myindex")
        assert method == "DELETE"
        assert path == "/myindex"

    def test_cat_endpoint_gets_format_json_appended(self) -> None:
        _, path, _ = self._parse_query("GET /_cat/indices")
        assert "format=json" in path

    def test_cat_endpoint_already_has_format_json(self) -> None:
        _, path, _ = self._parse_query("GET /_cat/indices?format=json")
        # Should not be duplicated
        assert path.count("format=json") == 1

    def test_cat_endpoint_with_existing_query_param(self) -> None:
        _, path, _ = self._parse_query("GET /_cat/indices?v=true")
        assert "format=json" in path
        assert "&format=json" in path

    def test_non_cat_endpoint_no_format_appended(self) -> None:
        _, path, _ = self._parse_query("GET /myindex/_search")
        assert "format=json" not in path

    def test_put_with_body(self) -> None:
        query = 'PUT /myindex\n{"settings": {"number_of_shards": 1}}'
        method, path, body = self._parse_query(query)
        assert method == "PUT"
        assert path == "/myindex"
        assert body["settings"]["number_of_shards"] == 1
