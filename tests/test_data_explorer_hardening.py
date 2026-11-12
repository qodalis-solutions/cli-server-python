"""Data explorer hardening: per-source permissions, read-only sources, allowed
objects, masking, audit and rate limits.

Spec: web-cli ``docs/superpowers/specs/2026-11-12-data-explorer-hardening-design.md``.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# The database drivers may be missing: mock them before the providers load.
for _mod in ("asyncpg", "aiomysql", "pymssql"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = MagicMock()

# The data explorer plugins are not pip-installed in CI: import their sources.
_PLUGINS_ROOT = os.path.join(os.path.dirname(__file__), "..", "plugins")
for _plugin in (
    "data-explorer",
    "data-explorer-sql",
    "data-explorer-postgres",
    "data-explorer-mysql",
    "data-explorer-mssql",
):
    _path = os.path.join(_PLUGINS_ROOT, _plugin)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from qodalis_cli import CliServerOptions, create_cli_server  # noqa: E402
from qodalis_cli_audit.in_memory_audit_store import InMemoryAuditStore  # noqa: E402
from qodalis_cli_data_explorer import (  # noqa: E402
    MASK,
    DataExplorerExecutor,
    DataExplorerRateLimiter,
    DataExplorerRegistry,
    apply_column_policy,
    filter_schema,
    is_object_allowed,
    masked_column_error,
    masked_result_error,
    objects_error,
    read_only_error,
    sql_objects,
)
from qodalis_cli_data_explorer_mssql import mssql_provider as mssql_mod  # noqa: E402
from qodalis_cli_data_explorer_mysql import mysql_provider as mysql_mod  # noqa: E402
from qodalis_cli_data_explorer_postgres import postgres_provider as pg_mod  # noqa: E402
from qodalis_cli_data_explorer_sql import SqlDataExplorerProvider  # noqa: E402
from qodalis_cli_server_abstractions import (  # noqa: E402
    DataExplorerCaller,
    DataExplorerExecutionContext,
    DataExplorerLanguage as L,
    DataExplorerProviderOptions,
    DataExplorerRateLimit,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)
from qodalis_cli_users import CliUsersBuilder  # noqa: E402

ROOT_PASS = "rootpass1"
V1 = "/api/v1/qcli"
DE = "/api/qcli/data-explorer"
MCP = "/api/qcli/mcp"


def opts(**kw: Any) -> DataExplorerProviderOptions:
    kw.setdefault("name", "db")
    kw.setdefault("description", "test db")
    return DataExplorerProviderOptions(**kw)


def result(columns: list[str] | None, rows: list[Any]) -> DataExplorerResult:
    return DataExplorerResult(
        success=True, source="db", language=L.SQL, default_output_format="table",  # type: ignore[arg-type]
        execution_time=0, columns=columns, rows=rows, row_count=len(rows), truncated=False, error=None,
    )


class FakeProvider(IDataExplorerProvider):
    """Answers every query with one row (``id``, ``email``, ``password``)."""

    def __init__(self, read_only_queries: bool = False) -> None:
        self.queries: list[str] = []
        self.read_only_queries = read_only_queries

    async def execute_async(self, context: DataExplorerExecutionContext) -> DataExplorerResult:
        self.queries.append(context.query)
        return DataExplorerResult(
            success=True, source=context.options.name, language=context.options.language,
            default_output_format=context.options.default_output_format, execution_time=1,
            columns=["id", "email", "password"], rows=[[1, "a@x.io", "h1"]], row_count=1, truncated=False, error=None,
        )

    async def get_schema_async(self, options: DataExplorerProviderOptions) -> DataExplorerSchemaResult:
        return DataExplorerSchemaResult(source=options.name, tables=[
            DataExplorerSchemaTable("users", "table", [
                DataExplorerSchemaColumn("id", "int"), DataExplorerSchemaColumn("password", "text"),
            ]),
            DataExplorerSchemaTable("secrets", "table", []),
        ])


# ---------------------------------------------------------------------------
# Read-only rules
# ---------------------------------------------------------------------------


class TestReadOnlyRules:
    @pytest.mark.parametrize("q", [
        "SELECT * FROM users",
        "select 1; ",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT 'DELETE FROM users' AS s",
        "EXPLAIN SELECT * FROM users",
        "-- DROP TABLE users\nSELECT 1",
        "SHOW TABLES",
        "SELECT replace(name, 'a', 'b') FROM users",
        "SELECT 'it''s'",
        "SELECT 1 # trailing MySQL comment",
        "SELECT E'it\\'s' AS s",
        'SELECT "a""b", [c]]d], `e``f` FROM t',
        "SELECT 1 /* a /* nested */ b */",
        "SELECT $1::int",
    ])
    def test_sql_reads(self, q: str) -> None:
        assert read_only_error(L.SQL, q) is None

    @pytest.mark.parametrize("q", [
        "DELETE FROM users",
        "update users set a = 1",
        "INSERT INTO users VALUES (1)",
        "DROP TABLE users",
        "SELECT 1; DELETE FROM users",
        "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x",
        "SELECT * INTO copy FROM users",
        "TRUNCATE users",
        "CREATE TABLE t (a int)",
        "PRAGMA writable_schema = 1",
        # A backslash or dollar quote one dialect reads as a string another runs as code
        "SELECT 'a\\'; DROP TABLE users; --'",
        "SELECT * FROM #t; DROP TABLE users",
        "SELECT $$; DROP TABLE users; $$",
        # MySQL: ``--`` needs a space (``1 --1`` is ``1 - -1``), /*! ... */ runs, "..." is a string
        "SELECT 1 --1; COMMIT; DELETE FROM users",
        "SELECT 1 /*! INTO OUTFILE '/tmp/x' */",
        "SELECT \"\\\"\" INTO OUTFILE '/tmp/f' -- \"",
        # Postgres and SQL Server comments nest; SQL Server escapes ] as ]]
        "SELECT 1 /* /* */ ' */ COMMIT DROP TABLE users --'",
        "SELECT [x]]'] , 1 DROP TABLE users --'",
        # # is a comment only in MySQL
        "SELECT 1 # x\n; DROP TABLE users",
    ])
    def test_sql_writes(self, q: str) -> None:
        assert read_only_error(L.SQL, q) is not None

    def test_mongo(self) -> None:
        assert read_only_error(L.SHELL, "db.users.find({})") is None
        assert read_only_error(L.SHELL, "show collections") is None
        assert read_only_error(L.SHELL, "db.users.insertOne({a:1})") is not None
        assert read_only_error(L.SHELL, "db.users.drop()") is not None
        assert read_only_error(L.SHELL, 'db.users.aggregate([{ $out: "x" }])') is not None
        # MongoDB sources registered with the json language follow the same rules
        assert read_only_error(L.JSON, "db.users.find({})") is None
        assert read_only_error(L.JSON, "db.users.deleteMany({})") is not None

    def test_redis(self) -> None:
        assert read_only_error(L.REDIS, "GET rates:usd") is None
        assert read_only_error(L.REDIS, "hgetall h") is None
        assert read_only_error(L.REDIS, "SET a 1") is not None
        assert read_only_error(L.REDIS, "FLUSHALL") is not None

    def test_elasticsearch(self) -> None:
        assert read_only_error(L.ELASTICSEARCH, "GET /logs/_search") is None
        assert read_only_error(L.ELASTICSEARCH, "POST /logs/_search\n{}") is None
        assert read_only_error(L.ELASTICSEARCH, "DELETE /logs") is not None
        assert read_only_error(L.ELASTICSEARCH, "POST /logs/_doc\n{}") is not None

    def test_other_languages_are_refused(self) -> None:
        assert "read_only=False" in (read_only_error(L.GRAPHQL, "{ users { id } }") or "")


# ---------------------------------------------------------------------------
# Allowed objects and columns
# ---------------------------------------------------------------------------


class TestAllowedObjects:
    def test_sql_objects(self) -> None:
        assert sorted(sql_objects("SELECT * FROM public.users u JOIN orders o ON o.uid = u.id")) == [
            "orders", "public.users",
        ]
        assert sql_objects("WITH x AS (SELECT 1) SELECT * FROM x") == []
        assert sql_objects("SELECT EXTRACT(YEAR FROM day) FROM t") == ["t"]
        assert sql_objects('SELECT * FROM "Users", [dbo].[orders]') == ["Users", "dbo.orders"]
        # Read as each dialect reads its comments and brackets
        assert "secret" in sql_objects("SELECT * FROM allowed /* /* */ ' */ , secret --'")
        assert "secret" in sql_objects("SELECT arr[(SELECT 1 FROM secret)] FROM allowed")

    @pytest.mark.parametrize("q", [
        # Found by the Node review: a quote or comment one dialect skips hides the table from another
        r"SELECT 1 $x$, '\'' a FROM secret WHERE ''='' -- $x$",
        r'SELECT "\"" a FROM secret WHERE ""=""',
        "SELECT 1 #'\n FROM secret -- '",
        "SELECT a--1, b FROM secret",
        "SELECT 1 /*! FROM secret */",
        r"SELECT '\', $x$ ' $x$ FROM secret -- '",
        "SELECT $a1$ ' $a1$ FROM secret -- '",
        r"SELECT E'\'' FROM secret -- '",
        "SELECT 1 # 2 FROM secret",
        "SELECT (ARRAY[1,2])[(SELECT length(']'))] FROM secret",
    ])
    def test_objects_under_every_reading(self, q: str) -> None:
        assert "secret" in sql_objects(q)
        assert objects_error(opts(allowed_objects=["allowed"]), q) == "'secret' is not available on this source"

    @pytest.mark.parametrize("q", [
        # MySQL without ANSI_QUOTES reads "Users" as a string; the table is still Users, never WHERE or the alias
        """SELECT "Id" FROM "Users" WHERE "CountryCode" = 'RO'""",
        """SELECT u."Id" FROM "Users" u WHERE u."CountryCode" = 'RO' ORDER BY 1""",
        'SELECT * FROM "Users" AS u JOIN "Bots" b ON b."UserId" = u."Id"',
    ])
    def test_quoted_names_of_allowed_tables(self, q: str) -> None:
        assert objects_error(opts(allowed_objects=["Users", "Bots"]), q) is None

    def test_patterns(self) -> None:
        assert is_object_allowed("public.users", ["users"])
        assert is_object_allowed("public.users", ["public.*"])
        assert not is_object_allowed("audit.users", ["public.*"])
        assert is_object_allowed("rates:usd", ["rates:*"])
        assert is_object_allowed("USERS", ["users"])

    def test_refuses_other_objects(self) -> None:
        o = opts(allowed_objects=["users", "orders"])
        assert objects_error(o, "SELECT * FROM users") is None
        assert "'secrets'" in (objects_error(o, "SELECT * FROM users JOIN secrets ON 1=1") or "")
        assert "session:1" in (objects_error(opts(language=L.REDIS, allowed_objects=["rates:*"]), "GET session:1") or "")
        assert "secrets" in (objects_error(opts(language=L.SHELL, allowed_objects=["users"]), "db.secrets.find()") or "")
        # SCAN without MATCH may touch any key
        assert objects_error(opts(language=L.REDIS, allowed_objects=["rates:*"]), "SCAN 0") is not None
        assert objects_error(opts(language=L.ELASTICSEARCH, allowed_objects=["logs-*"]), "GET /logs-2026/_search") is None

    def test_filter_schema(self) -> None:
        schema = DataExplorerSchemaResult(source="db", tables=[
            DataExplorerSchemaTable("users", "table", [
                DataExplorerSchemaColumn("id", "int"), DataExplorerSchemaColumn("password", "text"),
            ]),
            DataExplorerSchemaTable("secrets", "table", []),
        ])
        filtered = filter_schema(schema, opts(allowed_objects=["users"], drop_columns=["password"]))
        assert [t.name for t in filtered.tables] == ["users"]
        assert [c.name for c in filtered.tables[0].columns] == ["id"]
        # The provider's schema is not changed
        assert len(schema.tables) == 2 and len(schema.tables[0].columns) == 2

    def test_mask_and_drop(self) -> None:
        o = opts(mask_columns=["email"], drop_columns=["pass*"])
        table = apply_column_policy(result(["id", "email", "password"], [[1, "a@x.io", "h1"], [2, None, "h2"]]), o)
        assert table.columns == ["id", "email"]
        assert table.rows == [[1, MASK], [2, None]]
        docs = apply_column_policy(
            result(None, [{"id": 1, "email": "a@x.io", "password": "h", "nested": {"email": "b@x.io"}}]), o,
        )
        assert docs.rows == [{"id": 1, "email": MASK, "nested": {"email": MASK}}]


class TestParityRules:
    """Rules shared with the Node and .NET servers (same inputs as their tests)."""

    NAMES_THE_OBJECTS = "This source only accepts queries that name the objects they read"

    @pytest.mark.parametrize("q", [
        "SELECT $a1$ ' $a1$; DELETE FROM users; -- '",
        "SELECT 1 COMMIT",
        "SELECT 1 ROLLBACK",
        "SELECT 1 /* /* */ ' */ COMMIT DROP TABLE users --'",
        "SELECT 1 DENY SELECT ON users TO public",
        "SELECT 1 --x\r; DELETE FROM users",
    ])
    def test_sql_refused(self, q: str) -> None:
        assert read_only_error(L.SQL, q) is not None

    @pytest.mark.parametrize("q", ["SELECT $1::int", "SELECT café FROM t", "SELECT 1\r\nFROM t"])
    def test_sql_accepted(self, q: str) -> None:
        assert read_only_error(L.SQL, q) is None

    def test_error_texts(self) -> None:
        assert read_only_error(L.SQL, "SELECT 1 COMMIT") == "Read-only source: COMMIT is not allowed"
        assert read_only_error(L.SQL, "SELECT 1 --x\r; DELETE FROM users") == \
            "Read-only source: end lines with \\n or \\r\\n"
        assert objects_error(opts(allowed_objects=["t"]), "SELECT 1 --x\r FROM t") == self.NAMES_THE_OBJECTS

    @pytest.mark.parametrize("q", [
        "SELECT $a1$ ' $a1$ FROM secret -- '",
        "SELECT * FROM (SELECT 1) a, secret",
        "SELECT * FROM allowed a, generate_series(1, 2) g, secret",
        "DELETE FROM allowed USING secret WHERE 1=1",
        "SELECT * FROM allowed WHERE EXISTS (WITH secret AS (SELECT 1) SELECT 1) UNION SELECT * FROM secret",
        "SELECT * FROM allowed..secret",
        "DESCRIBE secret",
        "EXPLAIN secret",
    ])
    def test_sql_exposes_secret(self, q: str) -> None:
        assert any(o.split(".")[-1] == "secret" for o in sql_objects(q)), sql_objects(q)
        assert objects_error(opts(allowed_objects=["allowed"]), q) is not None

    def test_sql_objects(self) -> None:
        assert sql_objects("SELECT * FROM état") == ["état"]
        assert sql_objects("SELECT EXTRACT(YEAR FROM day) AS y") == []
        assert sql_objects("WITH recent AS (SELECT * FROM users) SELECT * FROM recent") == ["users"]
        assert sql_objects("EXPLAIN ANALYZE SELECT 1") == []
        assert sql_objects('SELECT * FROM "sec""ret"') == ['sec"ret']

    def test_mongo(self) -> None:
        escaped = 'db.users.aggregate([{ "\\u0024out": "x" }])'
        assert read_only_error(L.SHELL, escaped) == "Read-only source: $out and $merge are not allowed"
        o = opts(language=L.SHELL, allowed_objects=["users"])
        assert objects_error(o, "db.users.aggregate([{ $match: {} }])") is None
        for q in [
            'db.users.aggregate([{ $lookup: { from: "secret", localField: "a", foreignField: "b", as: "s" } }])',
            'db.users.aggregate([{ "$unionWith": "secret" }])',
            'db.users.aggregate([{ "$unionWith": { "coll": "secret" } }])',
            'db.users.aggregate([{ "\\u0024lookup": { "from": "secret" } }])',
            'db.users.aggregate([{ $graphLookup: { from: "secret", startWith: "$a", connectFromField: "a", '
            'connectToField: "b", as: "s" } }])',
        ]:
            assert objects_error(o, q) == "'secret' is not available on this source", q
        assert objects_error(o, 'db.users.aggregate([{ $lookup: { from: name, as: "s" } }])') == self.NAMES_THE_OBJECTS

    @pytest.mark.parametrize("q", [
        "GET /logs/_search", "POST /logs/_search\n{}", "POST /_all/_search\n{}", "POST /logs/_validate/query\n{}",
    ])
    def test_elasticsearch_reads(self, q: str) -> None:
        assert read_only_error(L.ELASTICSEARCH, q) is None

    @pytest.mark.parametrize("q", [
        "DELETE /logs", "POST /logs/_doc\n{}", "POST /logs/_delete_by_query#/_search\n{}", "POST /logs/_doc/_search\n{}",
    ])
    def test_elasticsearch_writes(self, q: str) -> None:
        assert read_only_error(L.ELASTICSEARCH, q) is not None

    def test_elasticsearch_objects(self) -> None:
        assert read_only_error(L.ELASTICSEARCH, "POST /logs/_delete_by_query#/_search\n{}") == \
            "Read-only source: POST /logs/_delete_by_query is not allowed"
        o = opts(language=L.ELASTICSEARCH, allowed_objects=["logs"])
        assert objects_error(o, 'POST /logs/_search\n{"query":{"match_all":{}}}') is None
        assert objects_error(o, 'POST /logs/_msearch\n{"index":"secret"}\n{}') == self.NAMES_THE_OBJECTS
        refused = objects_error(opts(language=L.ELASTICSEARCH, allowed_objects=["metrics-*"]), "GET /logs/_search")
        assert "logs" in (refused or "")

    def test_redis_keys(self) -> None:
        exact = opts(language=L.REDIS, allowed_objects=["rates:usd"])
        assert objects_error(exact, "GET rates:usd") is None
        assert objects_error(exact, "GET 'rates:usd'x") == "'rates:usdx' is not available on this source"
        # Split on any whitespace, as the provider does
        assert objects_error(exact, "MGET rates:usd\nsecret") == "'secret' is not available on this source"
        pattern = opts(language=L.REDIS, allowed_objects=["rates:*"])
        assert "session:1" in (objects_error(pattern, "GET session:1") or "")
        assert objects_error(pattern, "SCAN 0") == self.NAMES_THE_OBJECTS
        assert objects_error(pattern, 'MSET rates:a 1 "rates:b" 2') is None
        assert read_only_error(L.REDIS, "") == "Read-only source: this command is not allowed"

    def test_json_text_is_masked_like_a_document(self) -> None:
        o = opts(mask_columns=["email"], drop_columns=["pass*"])
        table = apply_column_policy(result(["response"], [
            ['{"_source":{"email":"a@x.io","password":"h","id":1}}'], ["[not json"], ['{"id":1}'],
        ]), o)
        assert table.rows == [['{"_source":{"email":"***","id":1}}'], ["[not json"], ['{"id":1}']]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class TestMaskedColumns:
    O = opts(mask_columns=["email", "*phone*"], drop_columns=["password_hash"])

    @pytest.mark.parametrize("q", [
        "SELECT * FROM users",
        "SELECT email FROM users",
        "SELECT DISTINCT u.email, name FROM users u",
        "SELECT TOP 5 email FROM users",
        "SELECT password_hash FROM users;",
        "SELECT * FROM phones",
        "SELECT p.number FROM phones AS p",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    ])
    def test_plain_selects(self, q: str) -> None:
        assert masked_column_error(self.O, q) is None

    @pytest.mark.parametrize("q", [
        "SELECT email AS e FROM users",
        "SELECT email e FROM users",
        "SELECT upper(email) FROM users",
        "SELECT email || '' FROM users",
        "SELECT id FROM users WHERE email = 'a@x.io'",
        "SELECT * FROM users u WHERE u.email LIKE 'a%'",
        "SELECT * FROM users ORDER BY email",
        "SELECT name AS email, email AS name FROM users",
        "SELECT id FROM t UNION SELECT email FROM users",
        "SELECT (SELECT email FROM users LIMIT 1)",
        "SELECT CASE WHEN home_phone LIKE '5%' THEN 1 END FROM users",
        "SELECT password_hash AS h FROM users",
        # under the MySQL reading the # comment hides nothing: the alias counts
        "SELECT email # x\nAS e FROM users",
        """SELECT "id" FROM "users" WHERE "email" LIKE '%a%'""",
        """SELECT "id" FROM "users" u WHERE u."email" = 'x'""",
    ])
    def test_other_uses_are_refused(self, q: str) -> None:
        assert masked_column_error(self.O, q) is not None
        assert "can only be selected by its own name" in (masked_column_error(self.O, q) or "")

    @pytest.mark.parametrize("q", [
        "SELECT u FROM users u",
        "SELECT row_to_json(u) FROM users AS u",
        "SELECT * FROM users WHERE users::text LIKE '%@%'",
        "WITH x AS (SELECT * FROM users) SELECT x FROM x",
    ])
    def test_whole_rows_are_refused(self, q: str) -> None:
        assert "is a whole row" in (masked_column_error(self.O, q) or "")

    def test_unicode_escaped_names_are_refused(self) -> None:
        assert "Unicode escapes" in (masked_column_error(self.O, 'SELECT U&"\\0065mail" AS e FROM users') or "")

    def test_only_sql_and_only_with_a_policy(self) -> None:
        assert masked_column_error(opts(), "SELECT email AS e FROM users") is None
        mongo = opts(language=L.SHELL, mask_columns=["email"])
        assert masked_column_error(mongo, "db.users.find({email: 'a'})") is None

    def test_result_must_carry_the_column(self) -> None:
        assert masked_result_error(self.O, "SELECT email FROM users", ["EMAIL"]) is None
        assert masked_result_error(self.O, "SELECT email FROM users FOR JSON PATH", ["JSON_F52E"]) is not None
        assert masked_result_error(self.O, "SELECT * FROM users", ["x"]) is None


def executor_for(o: DataExplorerProviderOptions, provider: FakeProvider | None = None, **kw: Any):
    provider = provider or FakeProvider()
    registry = DataExplorerRegistry()
    registry.register(provider, o)
    entries: list[Any] = []

    async def sink(entry: Any) -> None:
        entries.append(entry)

    executor = DataExplorerExecutor(registry, audit=lambda: sink, **kw)
    return executor, provider, entries, registry


def caller(user_id: str = "u1", perms: tuple[str, ...] = ()) -> DataExplorerCaller:
    return DataExplorerCaller(user_id=user_id, user_name=user_id, remote_address="10.0.0.1",
                              has_permission=lambda p: p in perms)


class TestExecutor:
    async def test_read_only_by_default(self) -> None:
        executor, provider, _, _ = executor_for(opts())
        res = await executor.execute_async({"source": "db", "query": "DELETE FROM users"})
        assert res.success is False and res.error_code == "read_only"
        assert provider.queries == []

    async def test_writes_when_not_read_only(self) -> None:
        executor, provider, _, _ = executor_for(opts(read_only=False))
        res = await executor.execute_async({"source": "db", "query": "DELETE FROM users"})
        assert res.success is True
        assert provider.queries == ["DELETE FROM users"]

    async def test_read_only_queries_provider_skips_the_check(self) -> None:
        executor, provider, _, _ = executor_for(opts(language=L.GRAPHQL), FakeProvider(read_only_queries=True))
        res = await executor.execute_async({"source": "db", "query": "{ users { id } }"})
        assert res.success is True

    async def test_permissions(self) -> None:
        executor, provider, _, _ = executor_for(opts(required_permissions=["data:billing"]))
        res = await executor.execute_async({"source": "db", "query": "SELECT 1"}, caller())
        assert res.error_code == "forbidden" and "data:billing" in (res.error or "")
        ok = await executor.execute_async({"source": "db", "query": "SELECT 1"}, caller(perms=("data:billing",)))
        assert ok.success is True
        # Open server: no has_permission, nothing enforced
        open_ = await executor.execute_async({"source": "db", "query": "SELECT 1"}, DataExplorerCaller())
        assert open_.success is True

    async def test_rate_limit(self) -> None:
        now = [0.0]
        executor, _, _, _ = executor_for(
            opts(rate_limit=DataExplorerRateLimit(max_queries=2, window_seconds=60)),
            rate_limiter=DataExplorerRateLimiter(lambda: now[0]),
        )
        run = lambda who="u1": executor.execute_async({"source": "db", "query": "SELECT 1"}, caller(who))  # noqa: E731
        assert (await run()).success and (await run()).success
        limited = await run()
        assert limited.error_code == "rate_limited" and limited.error == "Too many queries: at most 2 per 60 s"
        assert (await run("u2")).success  # per caller
        now[0] = 61
        assert (await run()).success  # sliding window

    async def test_mask_and_audit(self) -> None:
        executor, _, entries, _ = executor_for(opts(mask_columns=["email"], drop_columns=["password"]))
        res = await executor.execute_async({"source": "db", "query": "SELECT * FROM users"}, caller())
        assert res.columns == ["id", "email"] and res.rows == [[1, MASK]]
        await executor.execute_async({"source": "db", "query": "DROP TABLE users"}, caller())
        assert [e.event for e in entries] == ["data_query", "data_query"]
        assert entries[0].user_id == "u1"
        assert entries[0].metadata["source"] == "db" and entries[0].metadata["success"] is True
        assert entries[0].metadata["rowCount"] == 1 and "durationMs" in entries[0].metadata
        assert entries[0].metadata["remoteAddress"] == "10.0.0.1"
        assert entries[1].metadata["success"] is False and entries[1].metadata["error"].startswith("Read-only source")

    async def test_masked_columns_cannot_be_renamed(self) -> None:
        executor, provider, entries, _ = executor_for(opts(mask_columns=["email"]))
        res = await executor.execute_async({"source": "db", "query": "SELECT email AS e FROM users"}, caller())
        assert not res.success and res.error_code == "masked_column"  # type: ignore[attr-defined]
        assert res.error == "Masked column 'email' can only be selected by its own name"
        assert provider.queries == [] and entries[0].metadata["error"] == res.error

    async def test_rows_under_another_name_are_withheld(self) -> None:
        class JsonProvider(FakeProvider):
            async def execute_async(self, context: DataExplorerExecutionContext) -> DataExplorerResult:
                return result(["JSON_F52E"], [['[{"email":"a@x.io"}]']])

        executor, _, _, _ = executor_for(opts(mask_columns=["email"]), JsonProvider())
        res = await executor.execute_async({"source": "db", "query": "SELECT email FROM users FOR JSON PATH"})
        assert not res.success and res.rows == [] and res.error_code == "masked_column"  # type: ignore[attr-defined]

    async def test_audit_truncates_and_can_be_off(self) -> None:
        executor, _, entries, _ = executor_for(opts())
        await executor.execute_async({"source": "db", "query": "SELECT " + "x" * 3000})
        assert len(entries[0].metadata["query"]) == 2001 and entries[0].metadata["query"].endswith("…")
        assert entries[0].user_name == "anonymous"
        executor, _, entries, _ = executor_for(opts(audit=False))
        await executor.execute_async({"source": "db", "query": "SELECT 1"})
        assert entries == []

    async def test_failing_audit_does_not_fail_the_query(self) -> None:
        registry = DataExplorerRegistry()
        registry.register(FakeProvider(), opts())

        async def broken(entry: Any) -> None:
            raise RuntimeError("audit down")

        res = await DataExplorerExecutor(registry, audit=lambda: broken).execute_async({"source": "db", "query": "SELECT 1"})
        assert res.success is True

    def test_sources(self) -> None:
        registry = DataExplorerRegistry()
        registry.register(FakeProvider(), opts(name="main", export_permissions=["data:export"]))
        registry.register(FakeProvider(), opts(name="billing", required_permissions=["data:billing"], read_only=False))
        plain = registry.get_sources(caller())
        assert [s["name"] for s in plain] == ["main"]
        assert plain[0]["read_only"] is True and plain[0]["can_export"] is False
        assert "mask_columns" not in plain[0] and "required_permissions" not in plain[0]
        full = registry.get_sources(caller(perms=("data:billing", "data:export")))
        assert [(s["name"], s["read_only"], s["can_export"]) for s in full] == [
            ("main", True, True), ("billing", False, True),
        ]
        assert len(registry.get_sources()) == 2


# ---------------------------------------------------------------------------
# HTTP routes and MCP tools on a protected server
# ---------------------------------------------------------------------------


@pytest.fixture()
def protected():
    def configure(b):
        b.add_data_explorer_provider(FakeProvider(), opts(name="main", export_permissions=["data:export"],
                                                          allowed_objects=["users"], drop_columns=["password"]))
        b.add_data_explorer_provider(FakeProvider(), opts(
            name="billing", required_permissions=["data:billing"],
            rate_limit=DataExplorerRateLimit(max_queries=2, window_seconds=60),
        ))

    result = create_cli_server(CliServerOptions(configure=configure, mcp=True))
    store = InMemoryAuditStore()
    result.mount_plugin(
        CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(1000).set_audit_store(store).build()
    )
    with TestClient(result.app) as client:
        def login(name: str, password: str) -> dict[str, str]:
            res = client.post(V1 + "/sessions", json={"username": name, "password": password})
            assert res.status_code == 201, res.text
            return {"Authorization": f"Bearer {res.json()['token']}"}

        root = login("root", ROOT_PASS)
        for group, perms in (("analysts", ["data:query", "data:billing"]), ("queriers", ["data:query"])):
            assert client.post(V1 + "/groups", headers=root, json={"name": group, "permissions": perms}).status_code == 201
        for name, group in (("analyst", "analysts"), ("plain", "queriers")):
            res = client.post(V1 + "/users", headers=root, json={
                "name": name, "email": f"{name}@x.io", "password": f"{name}pass1", "groups": [group, "users"],
            })
            assert res.status_code == 201, res.text
        users = {"root": root, "analyst": login("analyst", "analystpass1"), "plain": login("plain", "plainpass1")}
        yield client, users, store


def mcp_call(client: TestClient, headers: dict[str, str], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    res = client.post(MCP, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments},
    })
    assert res.status_code == 200, res.text
    return res.json()["result"]


class TestProtectedServer:
    def test_sources(self, protected) -> None:
        client, users, _ = protected
        plain = client.get(DE + "/sources", headers=users["plain"]).json()
        assert [s["name"] for s in plain] == ["main"]
        assert plain[0]["readOnly"] is True and plain[0]["canExport"] is False
        assert "allowedObjects" not in plain[0]
        root = client.get(DE + "/sources", headers=users["root"]).json()
        assert sorted(s["name"] for s in root) == ["billing", "main"]
        assert next(s for s in root if s["name"] == "main")["canExport"] is True

    def test_403_without_the_source_permissions(self, protected) -> None:
        client, users, _ = protected
        res = client.post(DE + "/execute", headers=users["plain"], json={"source": "billing", "query": "SELECT 1"})
        assert res.status_code == 403 and "data:billing" in res.json()["error"]
        assert client.get(DE + "/schema?source=billing", headers=users["plain"]).status_code == 403

    def test_schema_is_filtered(self, protected) -> None:
        client, users, _ = protected
        schema = client.get(DE + "/schema?source=main", headers=users["plain"]).json()
        assert [t["name"] for t in schema["tables"]] == ["users"]
        assert [c["name"] for c in schema["tables"][0]["columns"]] == ["id"]
        assert schema["tables"][0]["columns"][0]["primaryKey"] is False

    def test_refusals(self, protected) -> None:
        client, users, _ = protected
        res = client.post(DE + "/execute", headers=users["plain"], json={"source": "main", "query": "DELETE FROM users"})
        assert res.status_code == 200
        assert res.json()["success"] is False and res.json()["errorCode"] == "read_only"
        res = client.post(DE + "/execute", headers=users["plain"], json={"source": "main", "query": "SELECT * FROM secrets"})
        assert res.json()["errorCode"] == "object_not_allowed"
        ok = client.post(DE + "/execute", headers=users["plain"], json={"source": "main", "query": "SELECT * FROM users"})
        assert ok.json()["success"] is True and ok.json()["columns"] == ["id", "email"]
        assert "errorCode" not in ok.json()

    def test_429_over_the_rate_limit(self, protected) -> None:
        client, users, _ = protected

        def run():
            return client.post(DE + "/execute", headers=users["analyst"], json={"source": "billing", "query": "SELECT 1"})

        assert run().status_code == 200 and run().status_code == 200
        limited = run()
        assert limited.status_code == 429 and "Too many queries" in limited.json()["error"]

    async def test_audit_log(self, protected) -> None:
        client, users, store = protected
        assert client.post(DE + "/execute", headers=users["plain"],
                           json={"source": "main", "query": "SELECT 42 FROM users"}).status_code == 200
        entries = [e for e in await store.get_entries() if e.event == "data_query"]
        entry = next(e for e in entries if (e.metadata or {}).get("query") == "SELECT 42 FROM users")
        assert entry.username == "plain" and entry.actor == "plain" and entry.details == "main"
        assert entry.user_id and entry.metadata["source"] == "main" and entry.metadata["success"] is True

    def test_mcp_tools(self, protected) -> None:
        client, users, _ = protected
        sources = mcp_call(client, users["plain"], "data_sources", {})
        assert [s["name"] for s in sources["structuredContent"]["sources"]] == ["main"]
        schema = mcp_call(client, users["plain"], "data_schema", {"source": "billing"})
        assert schema["isError"] is True and "data:billing" in schema["content"][0]["text"]
        schema = mcp_call(client, users["plain"], "data_schema", {"source": "main"})
        assert [t["name"] for t in schema["structuredContent"]["tables"]] == ["users"]
        assert "primaryKey" in schema["structuredContent"]["tables"][0]["columns"][0]
        query = mcp_call(client, users["plain"], "data_query", {"source": "billing", "query": "SELECT 1"})
        assert query["isError"] is True and "data:billing" in query["content"][0]["text"]
        write = mcp_call(client, users["plain"], "data_query", {"source": "main", "query": "DELETE FROM users"})
        assert write["isError"] is True and write["structuredContent"]["errorCode"] == "read_only"


def test_open_server_ignores_permissions() -> None:
    def configure(b):
        b.add_data_explorer_provider(FakeProvider(), opts(name="billing", required_permissions=["data:billing"]))

    result = create_cli_server(CliServerOptions(configure=configure))
    with TestClient(result.app) as client:
        assert [s["name"] for s in client.get(DE + "/sources").json()] == ["billing"]
        res = client.post(DE + "/execute", json={"source": "billing", "query": "SELECT 1"})
        assert res.status_code == 200 and res.json()["success"] is True


# ---------------------------------------------------------------------------
# SQL providers: read-only transactions
# ---------------------------------------------------------------------------


def ctx(query: str, **kw: Any) -> DataExplorerExecutionContext:
    return DataExplorerExecutionContext(query=query, parameters={}, options=opts(**kw))


class TestSqliteReadOnly:
    async def test_query_only(self, tmp_path) -> None:
        path = str(tmp_path / "db.sqlite")
        provider = SqlDataExplorerProvider(path)
        created = await provider.execute_async(ctx("CREATE TABLE t (a int)", read_only=False))
        assert created.success is True
        refused = await provider.execute_async(ctx("INSERT INTO t VALUES (1)"))
        assert refused.success is False and "readonly" in (refused.error or "").replace("-", "").lower()
        assert (await provider.execute_async(ctx("INSERT INTO t VALUES (1)", read_only=False))).success
        read = await provider.execute_async(ctx("SELECT a FROM t"))
        assert read.rows == [[1]]


class _PgStatement:
    def __init__(self, rows: bool = True) -> None:
        self.rows = rows

    def get_attributes(self):
        attr = MagicMock()
        attr.name = "id"
        return [attr] if self.rows else []

    async def fetch(self, *args: Any):
        return [[1]] if self.rows else []

    def get_statusmsg(self) -> str:
        return "UPDATE 2"


class _PgConnection:
    def __init__(self, fail: bool = False, rows: bool = True) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.rows = rows

    async def execute(self, query: str, *args: Any):
        self.calls.append(query)
        return "OK"

    async def prepare(self, query: str):
        self.calls.append(query)
        if self.fail:
            raise RuntimeError("cannot execute DELETE in a read-only transaction")
        return _PgStatement(self.rows)

    async def close(self) -> None:
        self.calls.append("close")


class TestPostgresReadOnly:
    async def test_read_only_transaction(self, monkeypatch) -> None:
        conn = _PgConnection()

        async def connect(*a: Any, **k: Any):
            return conn

        monkeypatch.setattr(pg_mod.asyncpg, "connect", connect, raising=False)
        res = await pg_mod.PostgresDataExplorerProvider("postgresql://x").execute_async(ctx("SELECT 1 AS id"))
        assert res.success is True
        assert conn.calls == ["BEGIN READ ONLY", "SELECT 1 AS id", "ROLLBACK", "close"]

    async def test_rolls_back_on_failure(self, monkeypatch) -> None:
        conn = _PgConnection(fail=True)

        async def connect(*a: Any, **k: Any):
            return conn

        monkeypatch.setattr(pg_mod.asyncpg, "connect", connect, raising=False)
        res = await pg_mod.PostgresDataExplorerProvider("postgresql://x").execute_async(ctx("DELETE FROM t"))
        assert res.success is False and "read-only transaction" in (res.error or "")
        assert conn.calls == ["BEGIN READ ONLY", "DELETE FROM t", "ROLLBACK", "close"]

    async def test_no_transaction_when_writable(self, monkeypatch) -> None:
        conn = _PgConnection()

        async def connect(*a: Any, **k: Any):
            return conn

        monkeypatch.setattr(pg_mod.asyncpg, "connect", connect, raising=False)
        await pg_mod.PostgresDataExplorerProvider("postgresql://x").execute_async(ctx("SELECT 1 AS id", read_only=False))
        assert conn.calls == ["SELECT 1 AS id", "close"]

    async def test_statements_without_rows_run_prepared(self, monkeypatch) -> None:
        # Never through conn.execute (the simple protocol runs several statements)
        conn = _PgConnection(rows=False)

        async def connect(*a: Any, **k: Any):
            return conn

        monkeypatch.setattr(pg_mod.asyncpg, "connect", connect, raising=False)
        res = await pg_mod.PostgresDataExplorerProvider("postgresql://x").execute_async(
            ctx("UPDATE t SET a = 1", read_only=False),
        )
        assert res.success is True and res.rows == [[2]]
        assert conn.calls == ["UPDATE t SET a = 1", "close"]


class _MysqlCursor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.description = [("id",)]
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        self.calls.append(query)

    async def fetchall(self):
        return [(1,)]


class _MysqlConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cursor(self):
        return _MysqlCursor(self.calls)

    async def rollback(self) -> None:
        self.calls.append("ROLLBACK")

    async def commit(self) -> None:
        self.calls.append("COMMIT")

    def close(self) -> None:
        self.calls.append("close")


class TestMysqlReadOnly:
    async def test_read_only_transaction(self, monkeypatch) -> None:
        conn = _MysqlConnection()

        async def connect(*a: Any, **k: Any):
            return conn

        monkeypatch.setattr(mysql_mod.aiomysql, "connect", connect, raising=False)
        res = await mysql_mod.MysqlDataExplorerProvider("mysql://u:p@h/db").execute_async(ctx("SELECT 1 AS id"))
        assert res.success is True
        assert conn.calls == ["START TRANSACTION READ ONLY", "SELECT 1 AS id", "ROLLBACK", "close"]


class _MssqlCursor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.description = [("id",)]

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append(query)

    def fetchall(self):
        return [(1,)]


class _MssqlConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cursor(self):
        return _MssqlCursor(self.calls)

    def rollback(self) -> None:
        self.calls.append("ROLLBACK")

    def commit(self) -> None:
        self.calls.append("COMMIT")

    def close(self) -> None:
        self.calls.append("close")


class TestMssqlReadOnly:
    async def test_transaction_is_rolled_back(self, monkeypatch) -> None:
        conn = _MssqlConnection()
        monkeypatch.setattr(mssql_mod.pymssql, "connect", lambda **k: conn, raising=False)
        provider = mssql_mod.MssqlDataExplorerProvider("Server=h;Database=d;User Id=u;Password=p")
        res = await provider.execute_async(ctx("SELECT 1 AS id"))
        assert res.success is True
        assert conn.calls == ["SELECT 1 AS id", "ROLLBACK", "close"]

    async def test_writable_commits(self, monkeypatch) -> None:
        conn = _MssqlConnection()
        monkeypatch.setattr(mssql_mod.pymssql, "connect", lambda **k: conn, raising=False)
        provider = mssql_mod.MssqlDataExplorerProvider("Server=h;Database=d;User Id=u;Password=p")

        class NoRows(_MssqlCursor):
            def __init__(self, calls: list[str]) -> None:
                super().__init__(calls)
                self.description = None
                self.rowcount = 3

        conn.cursor = lambda: NoRows(conn.calls)  # type: ignore[method-assign]
        res = await provider.execute_async(ctx("UPDATE t SET a = 1", read_only=False))
        assert res.success is True and conn.calls == ["UPDATE t SET a = 1", "COMMIT", "close"]
