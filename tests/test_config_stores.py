"""Config stores: in memory, JSON file and database (SQLite here; PostgreSQL with
QCLI_TEST_POSTGRES; the row lock of every dialect with a recording fake session):
versions, conflicts, removals, persistence.

Ported from cli-server-node ``src/__tests__/config-stores.test.ts``."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import pytest

from qodalis_cli_server_abstractions.config import (
    CliConfigConflictError,
    CliConfigStoredSection,
    ICliConfigStore,
    CliConfigStoreDescription,
    InMemoryConfigStore,
    JsonFileConfigStore,
    apply_cli_config_changes,
    describe_cli_config_store,
    describe_cli_config_store_line,
)
from qodalis_cli_databases import (
    CliDatabaseConfigStore,
    CliMigrationDatabaseOptions,
    CliMigrationRunner,
    cli_database_name,
    postgres_connection,
    sqlite_connection,
)


def section(values: Optional[dict] = None, version: int = 0) -> CliConfigStoredSection:
    return CliConfigStoredSection(values or {}, version)


def memory_db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


# ---------------------------------------------------------------------------
# The behaviour every store shares
# ---------------------------------------------------------------------------


@pytest.fixture(params=["InMemoryConfigStore", "JsonFileConfigStore", "CliDatabaseConfigStore (SQLite)"])
async def store(request, tmp_path) -> AsyncIterator[ICliConfigStore]:
    if request.param == "InMemoryConfigStore":
        yield InMemoryConfigStore()
    elif request.param == "JsonFileConfigStore":
        yield JsonFileConfigStore(tmp_path / "config.json")
    else:
        db = memory_db()
        s = CliDatabaseConfigStore(sqlite_connection(db))
        await s.ready()
        try:
            yield s
        finally:
            db.close()


class TestStoreContract:
    async def test_unknown_section_is_empty_at_version_0(self, store):
        assert await store.read("p", "s") == section({}, 0)

    async def test_writes_bump_version_by_one_and_keep_values(self, store):
        first = await store.write("p", "s", {"a": 1, "b": ["x"]})
        assert first == section({"a": 1, "b": ["x"]}, 1)
        second = await store.write("p", "s", {"c": {"deep": True}})
        assert second == section({"a": 1, "b": ["x"], "c": {"deep": True}}, 2)
        assert await store.read("p", "s") == second

    async def test_none_removes_a_key(self, store):
        await store.write("p", "s", {"a": 1, "b": 2})
        after = await store.write("p", "s", {"a": None})
        assert after.values == {"b": 2}
        assert after.version == 2

    async def test_sections_and_providers_are_independent(self, store):
        await store.write("p", "s1", {"a": 1})
        await store.write("p", "s2", {"a": 2})
        await store.write("q", "s1", {"a": 3})
        assert (await store.read("p", "s1")).values == {"a": 1}
        assert (await store.read("p", "s2")).values == {"a": 2}
        assert (await store.read("q", "s1")).values == {"a": 3}
        assert (await store.read("q", "s1")).version == 1

    async def test_stale_expected_version_is_conflict_with_current_version(self, store):
        await store.write("p", "s", {"a": 1}, 0)
        await store.write("p", "s", {"a": 2}, 1)
        with pytest.raises(CliConfigConflictError) as info:
            await store.write("p", "s", {"a": 3}, 1)
        assert info.value.current_version == "2"
        assert (await store.read("p", "s")).values == {"a": 2}

    async def test_first_write_with_expected_version_other_than_0_is_conflict(self, store):
        with pytest.raises(CliConfigConflictError):
            await store.write("p", "new", {"a": 1}, 3)
        assert await store.read("p", "new") == section({}, 0)

    async def test_returned_values_are_copies(self, store):
        written = await store.write("p", "s", {"list": ["a"]})
        written.values["list"].append("mutated")
        read = await store.read("p", "s")
        read.values["list"].append("again")
        assert (await store.read("p", "s")).values["list"] == ["a"]

    async def test_secrets_are_stored_as_given(self, store):
        await store.write("p", "s", {"apiKey": "sk_live_1"})
        assert (await store.read("p", "s")).values["apiKey"] == "sk_live_1"


# ---------------------------------------------------------------------------
# apply_cli_config_changes
# ---------------------------------------------------------------------------


def test_apply_changes_sets_replaces_removes_without_touching_input():
    values = {"a": 1, "b": 2}
    assert apply_cli_config_changes(values, {"a": 3, "b": None, "c": "x"}) == {"a": 3, "c": "x"}
    assert values == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# InMemoryConfigStore
# ---------------------------------------------------------------------------


def test_in_memory_store_is_runtime_source():
    assert InMemoryConfigStore().source == "runtime"


def test_in_memory_store_describes_itself_lost_on_restart():
    assert InMemoryConfigStore().describe() == CliConfigStoreDescription("memory", "In memory", "", False)
    assert InMemoryConfigStore().describe().to_dict() == {
        "kind": "memory", "title": "In memory", "detail": "", "persistent": False,
    }
    line = describe_cli_config_store_line(describe_cli_config_store(InMemoryConfigStore()))
    assert line == "In memory, changes are lost on restart"


class TestDescribeCliConfigStore:
    def test_without_describe_a_custom_store_named_after_its_class_persistent_unless_runtime(self):
        class RedisConfigStore(InMemoryConfigStore):
            source = "database"

        redis = RedisConfigStore()
        redis.describe = None  # type: ignore[assignment,method-assign]
        assert describe_cli_config_store(redis) == CliConfigStoreDescription(
            "custom", "Custom store", "RedisConfigStore", True,
        )

        class PlainStore(ICliConfigStore):
            source = "runtime"

            async def read(self, provider, section):
                return CliConfigStoredSection()

            async def write(self, provider, section, changes, expected_version=None):
                return CliConfigStoredSection({}, 1)

        assert describe_cli_config_store(PlainStore()) == CliConfigStoreDescription(
            "custom", "Custom store", "PlainStore", False,
        )

    def test_a_custom_description_is_used_as_given(self):
        class Redis(InMemoryConfigStore):
            source = "database"

            def describe(self):
                return CliConfigStoreDescription(kind="database", title="Redis", detail="db 3", persistent=True)

        assert describe_cli_config_store_line(describe_cli_config_store(Redis())) == "Redis (db 3), kept across restarts"


# ---------------------------------------------------------------------------
# JsonFileConfigStore
# ---------------------------------------------------------------------------


class TestJsonFileConfigStore:
    def test_describes_itself_by_path_relative_to_the_app_or_file_name_outside_it(self, tmp_path):
        assert JsonFileConfigStore(tmp_path / "qcli-demo-config.json").describe() == CliConfigStoreDescription(
            "file", "JSON file", "qcli-demo-config.json", True,
        )
        assert JsonFileConfigStore(os.path.join(os.getcwd(), "data", "config.json")).describe().detail == "data/config.json"
        assert JsonFileConfigStore("qcli-demo-config.json").describe().detail == "qcli-demo-config.json"

    async def test_file_source_persists_across_instances_in_documented_shape(self, tmp_path):
        file = tmp_path / "nested" / "config.json"
        store = JsonFileConfigStore(file)
        assert store.source == "file"
        await store.write("custom", "custom", {"motd": "hi"})
        assert json.loads(file.read_text("utf-8")) == {"custom": {"custom": {"values": {"motd": "hi"}, "version": 1}}}
        again = JsonFileConfigStore(file)
        assert await again.read("custom", "custom") == section({"motd": "hi"}, 1)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    async def test_writes_file_readable_by_owner_only(self, tmp_path):
        file = tmp_path / "config.json"
        await JsonFileConfigStore(file).write("p", "s", {"secret": "x"})
        assert stat.S_IMODE(os.stat(file).st_mode) == 0o600
        assert os.listdir(tmp_path) == ["config.json"]

    async def test_writes_through_temp_file_then_renames(self, tmp_path, monkeypatch):
        file = tmp_path / "config.json"
        replaced: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src: Any, dst: Any) -> None:
            src, dst = os.fspath(src), os.fspath(dst)
            # The temp file is complete and private before it takes the file's place
            assert os.path.dirname(src) == os.path.dirname(dst)
            assert src != dst
            if sys.platform != "win32":
                assert stat.S_IMODE(os.stat(src).st_mode) == 0o600
            assert json.loads(open(src, encoding="utf-8").read())["p"]["s"]["values"] == {"a": 1}
            replaced.append((src, dst))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy)
        await JsonFileConfigStore(file).write("p", "s", {"a": 1})
        assert len(replaced) == 1
        assert replaced[0][1] == os.fspath(file)
        assert replaced[0][0].endswith(".tmp")
        assert os.listdir(tmp_path) == ["config.json"]

    async def test_missing_or_empty_file_is_empty(self, tmp_path):
        file = tmp_path / "config.json"
        assert await JsonFileConfigStore(file).read("p", "s") == section({}, 0)
        file.write_text("  \n")
        assert await JsonFileConfigStore(file).read("p", "s") == section({}, 0)

    async def test_refuses_file_that_is_not_json(self, tmp_path):
        file = tmp_path / "config.json"
        file.write_text("{oops")
        with pytest.raises(Exception, match="is not valid JSON"):
            await JsonFileConfigStore(file).read("p", "s")

    async def test_ignores_malformed_sections_and_versions(self, tmp_path):
        file = tmp_path / "config.json"
        file.write_text(json.dumps({
            "p": {"s": {"values": [1], "version": -3}, "t": "x", "u": {"values": {"a": 1}, "version": 2}},
            "q": [],
        }))
        store = JsonFileConfigStore(file)
        assert await store.read("p", "s") == section({}, 0)
        assert await store.read("p", "t") == section({}, 0)
        assert await store.read("p", "u") == section({"a": 1}, 2)

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "getuid") and os.getuid() == 0),
        reason="needs POSIX permissions and a non-root user",
    )
    async def test_failed_write_leaves_file_and_cache_as_they_were(self, tmp_path):
        sub = tmp_path / "ro"
        sub.mkdir()
        file = sub / "config.json"
        store = JsonFileConfigStore(file)
        await store.write("p", "s", {"a": 1})
        os.chmod(sub, 0o500)
        try:
            with pytest.raises(Exception):
                await store.write("p", "s", {"a": 2})
            assert await store.read("p", "s") == section({"a": 1}, 1)
        finally:
            os.chmod(sub, 0o700)
        assert json.loads(file.read_text("utf-8")) == {"p": {"s": {"values": {"a": 1}, "version": 1}}}


# ---------------------------------------------------------------------------
# CliDatabaseConfigStore
# ---------------------------------------------------------------------------


def _tables(db: sqlite3.Connection) -> list[str]:
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


class _OfflinePool:
    """An asyncpg pool's shape: the DSN it was created with; the database is offline here."""

    def __init__(self, dsn: str) -> None:
        self._connect_args = ()
        self._connect_kwargs = {"dsn": dsn}

    def acquire(self) -> Any:
        raise RuntimeError("offline")


class _OfflineConnection:
    dialect = "mssql"

    def session(self) -> Any:
        raise RuntimeError("offline")


class TestDatabaseConfigStore:
    async def test_describes_itself_by_dialect_database_name_and_table_never_credentials(self):
        db = memory_db()
        memory = CliDatabaseConfigStore(sqlite_connection(db))
        await memory.ready()
        assert memory.describe() == CliConfigStoreDescription("database", "Database", "SQLite, table QcliConfig", True)
        named = CliDatabaseConfigStore(sqlite_connection(db), database="/var/lib/app/qcli.db", table_prefix="App")
        await named.ready()
        assert named.describe().detail == "SQLite database qcli.db, table AppConfig"
        db.close()

        # PostgreSQL from a pool's connection string: the name only
        connection = postgres_connection(_OfflinePool("postgresql://admin:s3cr3t@db.internal:5432/qcli?sslmode=require"))
        assert connection.database == "qcli"
        pg = CliDatabaseConfigStore(connection, schema="qcli")
        described = pg.describe()
        assert described.detail == "PostgreSQL database qcli, table qcli.QcliConfig"
        for secret in ("s3cr3t", "admin", "db.internal", "5432"):
            assert secret not in json.dumps(described.to_dict())
        assert cli_database_name("Server=db;Database=qcli;User Id=sa;Password=s3cr3t") is None
        assert cli_database_name("mysql://root:s3cr3t@localhost/shop") == "shop"
        assert cli_database_name("orders") == "orders"
        # A connection string given as the name is never shown
        sql_server = CliDatabaseConfigStore(
            _OfflineConnection(),  # type: ignore[arg-type]
            database="Server=db;Database=qcli;User Id=sa;Password=s3cr3t",
        )
        assert sql_server.describe().detail == "SQL Server, table QcliConfig"

    async def test_a_sqlite_file_is_described_by_its_name(self, tmp_path):
        db = sqlite3.connect(tmp_path / "app.db", check_same_thread=False)
        store = CliDatabaseConfigStore(sqlite_connection(db))
        assert store.describe().detail == "SQLite database app.db, table QcliConfig"
        db.close()

    async def test_database_source_creates_table_through_migration(self):
        db = memory_db()
        store = CliDatabaseConfigStore(sqlite_connection(db))
        await store.ready()
        assert store.source == "database"
        assert store.migration_source.name == "qcli-config"
        tables = _tables(db)
        assert "QcliConfig" in tables
        assert "QcliMigrations" in tables
        applied = db.execute('SELECT "Source", "Id" FROM "QcliMigrations"').fetchall()
        assert applied == [("qcli-config", "1")]
        db.close()

    async def test_one_row_per_section_with_values_as_json(self):
        db = memory_db()
        store = CliDatabaseConfigStore(sqlite_connection(db))
        await store.write("cors", "cors", {"allowedOrigins": ["https://a.example"]})
        rows = db.execute('SELECT "Provider", "Section", "Version", "Data" FROM "QcliConfig"').fetchall()
        assert len(rows) == 1
        assert rows[0][:3] == ("cors", "cors", 1)
        assert json.loads(rows[0][3]) == {"allowedOrigins": ["https://a.example"]}
        db.close()

    async def test_persists_across_stores_on_same_database(self, tmp_path):
        file = tmp_path / "config.db"
        a = sqlite3.connect(file, check_same_thread=False)
        await CliDatabaseConfigStore(sqlite_connection(a)).write("p", "s", {"x": 1})
        a.close()
        b = sqlite3.connect(file, check_same_thread=False)
        store = CliDatabaseConfigStore(sqlite_connection(b))
        assert await store.read("p", "s") == section({"x": 1}, 1)
        b.close()

    async def test_table_prefix_and_per_table_name(self):
        db = memory_db()
        await CliDatabaseConfigStore(sqlite_connection(db), table_prefix="App").write("p", "s", {"a": 1})
        await CliDatabaseConfigStore(sqlite_connection(db), tables={"Config": "Settings"}).write("p", "s", {"a": 1})
        tables = _tables(db)
        assert "AppConfig" in tables
        assert "Settings" in tables
        with pytest.raises(ValueError, match="invalid table name"):
            CliDatabaseConfigStore(sqlite_connection(db), tables={"Config": "bad name"})
        db.close()

    async def test_conflict_rolls_transaction_back(self):
        db = memory_db()
        store = CliDatabaseConfigStore(sqlite_connection(db))
        await store.write("p", "s", {"a": 1})
        with pytest.raises(CliConfigConflictError):
            await store.write("p", "s", {"a": 2}, 0)
        assert db.in_transaction is False
        assert await store.read("p", "s") == section({"a": 1}, 1)
        # Still usable
        assert (await store.write("p", "s", {"a": 3}, 1)).version == 2
        db.close()

    async def test_corrupt_data_reads_as_empty(self):
        db = memory_db()
        store = CliDatabaseConfigStore(sqlite_connection(db))
        await store.write("p", "s", {"a": 1})
        db.execute('UPDATE "QcliConfig" SET "Data" = ?', ("{nope",))
        assert await store.read("p", "s") == section({}, 1)
        db.close()

    async def test_can_be_shown_in_databases_plugin(self):
        db = memory_db()
        store = CliDatabaseConfigStore(sqlite_connection(db))
        await store.ready()
        runner = CliMigrationRunner("config", CliMigrationDatabaseOptions(store.connection)).add_source(
            store.migration_source,
        )
        states = await runner.status()
        assert [f"{s.source}:{s.id}:{s.status}" for s in states] == ["qcli-config:1:applied"]
        db.close()


# ---------------------------------------------------------------------------
# CliDatabaseConfigStore: the row lock and transaction of every dialect
# ---------------------------------------------------------------------------


class _RecordingSession:
    def __init__(self, dialect: str, log: list[str], row: Optional[dict]) -> None:
        self.dialect = dialect
        self._log = log
        self._row = row

    async def query(self, sql: str, params: Any = ()) -> list[dict]:
        self._log.append(sql)
        if sql.lstrip().upper().startswith("SELECT"):
            return [dict(self._row)] if self._row else []
        return []

    async def exec(self, script: str) -> None:
        self._log.append(script)


class _RecordingConnection:
    def __init__(self, dialect: str, row: Optional[dict] = None) -> None:
        self.dialect = dialect
        self.log: list[str] = []
        self.row = row

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_RecordingSession]:
        yield _RecordingSession(self.dialect, self.log, self.row)


def _fake_store(dialect: str, row: Optional[dict] = None) -> tuple[CliDatabaseConfigStore, _RecordingConnection]:
    conn = _RecordingConnection(dialect, row)
    store = CliDatabaseConfigStore(conn)  # type: ignore[arg-type]

    async def ready() -> None:  # the schema is not under test here
        return None

    store.ready = ready  # type: ignore[method-assign]
    return store, conn


_LOCKS = {
    "postgres": ("BEGIN", "COMMIT", "ROLLBACK"),
    "mysql": ("START TRANSACTION", "COMMIT", "ROLLBACK"),
    "mssql": ("BEGIN TRANSACTION", "COMMIT TRANSACTION", "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"),
    "sqlite": ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"),
}


class TestDatabaseConfigStoreDialects:
    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "mssql", "sqlite"])
    async def test_write_locks_the_row_in_a_transaction(self, dialect):
        store, conn = _fake_store(dialect)
        assert await store.write("p", "s", {"a": 1}) == section({"a": 1}, 1)
        begin, commit, _ = _LOCKS[dialect]
        assert conn.log[0] == begin
        select = conn.log[1]
        assert select.startswith("SELECT")
        if dialect in ("postgres", "mysql"):
            assert select.rstrip().endswith("FOR UPDATE")
            assert "UPDLOCK" not in select
        elif dialect == "mssql":
            assert "WITH (UPDLOCK, HOLDLOCK)" in select
            assert "FOR UPDATE" not in select
        else:
            assert "FOR UPDATE" not in select and "UPDLOCK" not in select
        assert conn.log[2].startswith("INSERT INTO")
        assert conn.log[-1] == commit

    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "mssql", "sqlite"])
    async def test_existing_row_is_updated(self, dialect):
        store, conn = _fake_store(dialect, {"Version": 4, "Data": json.dumps({"a": 1})})
        assert await store.write("p", "s", {"b": 2}, 4) == section({"a": 1, "b": 2}, 5)
        assert conn.log[2].startswith("UPDATE")
        assert conn.log[-1] == _LOCKS[dialect][1]

    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "mssql", "sqlite"])
    async def test_conflict_rolls_back(self, dialect):
        store, conn = _fake_store(dialect, {"Version": 4, "Data": "{}"})
        with pytest.raises(CliConfigConflictError) as info:
            await store.write("p", "s", {"b": 2}, 3)
        assert info.value.current_version == "4"
        assert conn.log[-1] == _LOCKS[dialect][2]
        assert not any(sql.startswith(("UPDATE", "INSERT")) for sql in conn.log)

    async def test_identifiers_are_quoted_per_dialect(self):
        for dialect, quoted in [("postgres", '"QcliConfig"'), ("mysql", "`QcliConfig`"), ("mssql", "[QcliConfig]")]:
            store, conn = _fake_store(dialect)
            await store.write("p", "s", {"a": 1})
            assert quoted in conn.log[1]


# ---------------------------------------------------------------------------
# CliDatabaseConfigStore (PostgreSQL)
# ---------------------------------------------------------------------------

PG = os.environ.get("QCLI_TEST_POSTGRES")


@pytest.mark.skipif(not PG, reason="QCLI_TEST_POSTGRES not set")
async def test_postgres_writes_reads_and_refuses_stale_version():
    import asyncpg

    from qodalis_cli_databases import postgres_connection

    pool = await asyncpg.create_pool(PG, min_size=1, max_size=6)
    table = f"QcliConfigTest{int(time.time() * 1000)}"
    try:
        store = CliDatabaseConfigStore(postgres_connection(pool), tables={"Config": table})
        await store.ready()
        assert await store.write("p", "s", {"a": 1}) == section({"a": 1}, 1)
        with pytest.raises(CliConfigConflictError):
            await store.write("p", "s", {"a": 2}, 0)
        # Concurrent writers never lose a change
        await asyncio.gather(*(store.write("p", "s", {f"k{i}": i}) for i in range(5)))
        final = await store.read("p", "s")
        assert final.version == 6
        assert sorted(final.values) == ["a", "k0", "k1", "k2", "k3", "k4"]
    finally:
        try:
            await pool.execute(f'DROP TABLE IF EXISTS "{table}"')
        except Exception:  # noqa: BLE001
            pass
        await pool.close()

