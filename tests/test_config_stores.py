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
    describe_cli_config_store_name,
)
from tests.config_store_contract import ConfigStoreContract
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


class TestStoreContract(ConfigStoreContract):
    @pytest.fixture(params=[
        "InMemoryConfigStore",
        "JsonFileConfigStore",
        "JsonFileConfigStore (a section of a settings file)",
        "CliDatabaseConfigStore (SQLite)",
    ])
    async def store(self, request, tmp_path) -> AsyncIterator[ICliConfigStore]:
        if request.param == "InMemoryConfigStore":
            yield InMemoryConfigStore()
        elif request.param == "JsonFileConfigStore":
            yield JsonFileConfigStore(tmp_path / "config.json")
        elif request.param.startswith("JsonFileConfigStore"):
            file = tmp_path / "app.json"
            file.write_text(json.dumps({"logging": {"level": "info"}, "app": {"name": "demo"}}, indent=4))
            yield JsonFileConfigStore(file, section="app.qcli")
        else:
            db = memory_db()
            s = CliDatabaseConfigStore(sqlite_connection(db))
            await s.ready()
            try:
                yield s
            finally:
                db.close()


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


class TestJsonFileConfigStoreSection:
    """A section of an app settings file."""

    ORIGINAL = {
        "ConnectionStrings": {"main": "Server=db;Password=keep-me"},
        "logging": {"level": "info", "targets": ["console", "file"]},
        "port": 8080,
        "flags": [True, False],
    }

    @pytest.fixture
    def file(self, tmp_path):
        file = tmp_path / "app.json"
        file.write_text(json.dumps(self.ORIGINAL, indent=4) + "\n")
        return file

    def test_describes_itself_by_the_file_and_the_section(self, file):
        assert JsonFileConfigStore(file, section="qcli").describe() == CliConfigStoreDescription(
            "file", "JSON file", "app.json, section qcli", True,
        )
        nested = JsonFileConfigStore(os.path.join(os.getcwd(), "config", "app.json"), section="qcli")
        assert nested.describe().detail == "config/app.json, section qcli"
        assert JsonFileConfigStore(file, section="app:qcli").describe().detail == "app.json, section app.qcli"
        assert describe_cli_config_store_line(JsonFileConfigStore(file, section="qcli").describe()) == (
            "JSON file (app.json, section qcli), kept across restarts"
        )
        assert describe_cli_config_store_name(JsonFileConfigStore(file, section="qcli").describe()) == (
            "JSON file (app.json, section qcli)"
        )

    async def test_keeps_the_settings_under_the_section_and_the_rest_of_the_file_as_it_was(self, file):
        await JsonFileConfigStore(file, section="qcli").write("jobs", "history", {"maxAge": "30d"})
        text = file.read_text("utf-8")
        parsed = json.loads(text)
        assert parsed == {**self.ORIGINAL, "qcli": {"jobs": {"history": {"values": {"maxAge": "30d"}, "version": 1}}}}
        assert list(parsed) == ["ConnectionStrings", "logging", "port", "flags", "qcli"]
        assert '\n    "logging": {\n        "level": "info"' in text
        assert text.endswith("}\n")

    async def test_keeps_tabs_and_a_missing_trailing_newline(self, file):
        file.write_text(json.dumps({"a": {"b": 1}}, indent="\t"))
        await JsonFileConfigStore(file, section="qcli").write("p", "s", {"x": 1})
        text = file.read_text("utf-8")
        assert '\n\t"a": {\n\t\t"b": 1' in text
        assert text.endswith("}")

    async def test_a_nested_section_is_created_inside_the_existing_object(self, file):
        file.write_text(json.dumps({"app": {"name": "demo"}, "other": 1}, indent=2))
        await JsonFileConfigStore(file, section="app:qcli").write("p", "s", {"a": 1})
        assert json.loads(file.read_text("utf-8")) == {
            "app": {"name": "demo", "qcli": {"p": {"s": {"values": {"a": 1}, "version": 1}}}}, "other": 1,
        }
        assert await JsonFileConfigStore(file, section="app.qcli").read("p", "s") == section({"a": 1}, 1)

    async def test_re_reads_the_rest_of_the_file_on_every_write_edits_made_meanwhile_are_kept(self, file):
        store = JsonFileConfigStore(file, section="qcli")
        await store.write("p", "s", {"a": 1})
        edited = json.loads(file.read_text("utf-8"))
        edited["port"] = 9090
        edited["added"] = "by hand"
        file.write_text(json.dumps(edited, indent=4))
        await store.write("p", "s", {"b": 2})
        after = json.loads(file.read_text("utf-8"))
        assert after["port"] == 9090
        assert after["added"] == "by hand"
        assert after["qcli"]["p"]["s"] == {"values": {"a": 1, "b": 2}, "version": 2}

    async def test_versions_persist_across_instances_and_a_stale_version_is_refused(self, file):
        await JsonFileConfigStore(file, section="qcli").write("p", "s", {"a": 1})
        restarted = JsonFileConfigStore(file, section="qcli")
        assert await restarted.read("p", "s") == section({"a": 1}, 1)
        with pytest.raises(CliConfigConflictError) as info:
            await restarted.write("p", "s", {"a": 2}, 0)
        assert info.value.current_version == "1"
        assert (await restarted.write("p", "s", {"a": 2}, 1)).version == 2
        assert json.loads(file.read_text("utf-8"))["qcli"]["p"]["s"]["version"] == 2

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and inodes")
    async def test_writes_atomically_and_the_file_keeps_its_mode(self, file, tmp_path):
        os.chmod(file, 0o644)
        before = os.stat(file).st_ino
        # A reader holding the old file sees it whole: it is replaced, never rewritten in place
        with open(file, encoding="utf-8") as held:
            await JsonFileConfigStore(file, section="qcli").write("p", "s", {"a": 1})
            assert json.loads(held.read()) == self.ORIGINAL
        assert os.stat(file).st_ino != before
        assert os.listdir(tmp_path) == ["app.json"]
        assert stat.S_IMODE(os.stat(file).st_mode) == 0o644

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    async def test_a_new_settings_file_is_created_with_the_section_readable_by_the_owner_only(self, tmp_path):
        fresh = tmp_path / "new" / "settings.json"
        await JsonFileConfigStore(fresh, section="qcli").write("p", "s", {"a": 1})
        assert json.loads(fresh.read_text("utf-8")) == {"qcli": {"p": {"s": {"values": {"a": 1}, "version": 1}}}}
        assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o600

    async def test_a_section_that_is_not_an_object_or_a_file_that_is_not_json_is_refused(self, file):
        file.write_text(json.dumps({"qcli": "oops", "app": [1]}))
        with pytest.raises(ValueError, match="qcli is not an object"):
            await JsonFileConfigStore(file, section="qcli").read("p", "s")
        with pytest.raises(ValueError, match="app is not an object"):
            await JsonFileConfigStore(file, section="app.qcli").read("p", "s")
        store = JsonFileConfigStore(file, section="fresh")
        assert await store.read("p", "s") == section({}, 0)
        file.write_text("{broken")
        with pytest.raises(ValueError, match="is not valid JSON"):
            await store.write("p", "s", {"a": 1})
        assert file.read_text("utf-8") == "{broken"
        # The failed change is not kept
        file.write_text("{}")
        assert await store.read("p", "s") == section({}, 0)

    def test_an_empty_part_of_the_section_path_is_refused(self, file):
        with pytest.raises(ValueError, match="invalid config file section: app..qcli"):
            JsonFileConfigStore(file, section="app..qcli")
        assert JsonFileConfigStore(file, section="  ").section_path == ()


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

