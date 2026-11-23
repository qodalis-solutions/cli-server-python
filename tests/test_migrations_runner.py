"""The migrations runner against real databases: SQLite always; PostgreSQL, MySQL and SQL Server when their
``QCLI_TEST_*`` variables are set (see ``migrations_targets``).  Each test uses its own table prefix, dropped
afterwards."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from qodalis_cli_migrations import (
    CliMigration,
    CliMigrationDatabaseOptions,
    CliMigrationError,
    CliMigrationRun,
    CliMigrationRunner,
    CliMigrationRunOptions as Opts,
    adopt_legacy_table,
    code_source,
    column_exists,
    sql_folder_source,
    table_exists,
)

from tests.migrations_targets import TARGETS, create_table, drop_prefixed, drop_table, q, unique


@pytest.fixture(params=[name for name, _ in TARGETS])
async def env(request):
    open_target = dict(TARGETS)[request.param]
    connection, close = await open_target()
    prefix = unique()

    def runner(*sources):
        r = CliMigrationRunner("main", CliMigrationDatabaseOptions(connection, table_prefix=prefix))
        for s in sources:
            r.add_source(s)
        return r

    yield connection, prefix, runner
    try:
        await drop_prefixed(connection, prefix)
    finally:
        await close()


async def test_applies_a_folder_in_order_records_it_and_has_nothing_to_do_afterwards(env):
    connection, prefix, runner = env
    d = connection.dialect
    directory = tempfile.mkdtemp(prefix="qcli-mig-")
    a, b = f"{prefix}A", f"{prefix}B"
    files = {
        "0002_create_b.up.sql": create_table(b)[d],
        "0001_create_a.up.sql": create_table(a)[d],
        "0001_create_a.down.sql": drop_table(a)[d],
        "0010_add_c.up.sql": create_table(f"{prefix}C")[d],
    }
    for name, sql in files.items():
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            f.write(sql)
    r = runner(sql_folder_source(directory))

    assert [(s.id, s.status, s.has_down) for s in await r.status()] == [
        ("0001", "pending", True), ("0002", "pending", False), ("0010", "pending", False),
    ]
    run = await r.up(Opts(actor="root", trigger="api"))
    assert isinstance(run, CliMigrationRun)
    assert run.status == "succeeded"
    assert [x["id"] for x in run.applied] == ["0001", "0002", "0010"]
    states = await r.status()
    assert all(s.status == "applied" and s.applied_by == "root" and isinstance(s.applied_at, str) for s in states)
    assert states[0].name == "create a"
    async with connection.session() as db:
        assert await table_exists(db, a)
        assert await table_exists(db, f"{prefix}Migrations")
        assert await table_exists(db, f"{prefix}MigrationRuns")

    again = await r.up(Opts(trigger="startup"))
    assert again.applied == []
    items, total = await r.runs(10, 0)
    assert total == 2
    first = items[1].to_dict()
    assert {k: first[k] for k in ("id", "direction", "status", "actor", "trigger")} == {
        "id": run.id, "direction": "up", "status": "succeeded", "actor": "root", "trigger": "api",
    }
    assert [x["id"] for x in first["applied"]] == ["0001", "0002", "0010"]


async def test_stops_at_to_and_a_dry_run_applies_nothing(env):
    connection, prefix, runner = env
    r = runner(code_source("app", [
        CliMigration("1", "one", create_table(f"{prefix}One")),
        CliMigration("2", "two", create_table(f"{prefix}Two")),
    ]))
    dry = await r.up(Opts(dry_run=True))
    assert "-- app:1 one (up)" in dry["sql"]
    assert f"{prefix}Two" in dry["sql"]
    assert all(s.status == "pending" for s in await r.status())

    run = await r.up(Opts(to="1"))
    assert [x["id"] for x in run.applied] == ["1"]
    assert [s.status for s in await r.status()] == ["applied", "pending"]
    with pytest.raises(CliMigrationError) as err:
        await r.up(Opts(to="9"))
    assert err.value.status == 404
    async with connection.session() as db:
        assert not await table_exists(db, f"{prefix}Two")


async def test_refuses_while_a_migration_changed_or_went_missing_unless_forced(env):
    _, prefix, runner = env
    one = CliMigration("1", "one", create_table(f"{prefix}One"))
    await runner(code_source("app", [one])).up()

    edited = runner(code_source("app", [
        CliMigration("1", "one", create_table(f"{prefix}One", ', "X" int')),
        CliMigration("2", "two", create_table(f"{prefix}Two")),
    ]))
    assert [s.status for s in await edited.status()] == ["changed", "pending"]
    with pytest.raises(CliMigrationError) as err:
        await edited.up()
    assert err.value.status == 409 and "app:1 (changed)" in err.value.message
    assert [x["id"] for x in (await edited.up(Opts(force=True))).applied] == ["2"]

    gone = runner(code_source("app", [CliMigration("2", "two", create_table(f"{prefix}Two"))]))
    assert [(s.id, s.status) for s in await gone.status()] == [("2", "applied"), ("1", "missing")]
    with pytest.raises(CliMigrationError) as err:
        await gone.up()
    assert err.value.status == 409


async def test_a_failing_migration_is_rolled_back_recorded_and_marked_failed_until_fixed(env):
    connection, prefix, runner = env
    t = f"{prefix}Bad"

    async def broken(db):
        await db.exec(create_table(t)[db.dialect])
        await db.exec("SELECT * FROM no_such_table_here")

    bad = runner(code_source("app", [
        CliMigration("1", "ok", create_table(f"{prefix}Ok")),
        CliMigration("2", "broken", broken),
    ]))
    run = await bad.up()
    assert run.status == "failed"
    assert [x["id"] for x in run.applied] == ["1"]
    assert run.failed == {"source": "app", "id": "2"}
    assert run.error.startswith("app:2 broken: ")
    assert [s.status for s in await bad.status()] == ["applied", "failed"]
    # MySQL commits DDL on its own: only the transactional databases undo the table
    if connection.dialect != "mysql":
        async with connection.session() as db:
            assert not await table_exists(db, t)

    async def fixed_step(db):
        if not await table_exists(db, t):
            await db.exec(create_table(t)[db.dialect])

    fixed = runner(code_source("app", [
        CliMigration("1", "ok", create_table(f"{prefix}Ok")),
        CliMigration("2", "broken", fixed_step),
    ]))
    assert (await fixed.up()).status == "succeeded"
    assert [s.status for s in await fixed.status()] == ["applied", "applied"]


async def test_rolls_back_with_the_down_steps_and_refuses_without_one(env):
    connection, prefix, runner = env
    r = runner(code_source("app", [
        CliMigration("1", "one", create_table(f"{prefix}One"), drop_table(f"{prefix}One")),
        CliMigration("2", "two", create_table(f"{prefix}Two"), drop_table(f"{prefix}Two")),
        CliMigration("3", "three", create_table(f"{prefix}Three"), drop_table(f"{prefix}Three")),
    ]))
    await r.up()
    dry = await r.down(Opts(to="1", dry_run=True))
    assert dry["sql"].index("app:3") < dry["sql"].index("app:2")
    run = await r.down(Opts(to="1", actor="root"))
    assert run.direction == "down"
    assert [x["id"] for x in run.applied] == ["3", "2"]
    assert [s.status for s in await r.status()] == ["applied", "pending", "pending"]
    async with connection.session() as db:
        assert not await table_exists(db, f"{prefix}Two")
        assert await table_exists(db, f"{prefix}One")
    assert [x["id"] for x in (await r.down(Opts(to="base"))).applied] == ["1"]

    no_down = runner(code_source("app", [CliMigration("1", "one", create_table(f"{prefix}Solo"))]))
    await no_down.up()
    with pytest.raises(CliMigrationError) as err:
        await no_down.down(Opts(to="base"))
    assert err.value.status == 409 and "no down step for app:1" in err.value.message


async def test_applies_sources_in_order_and_tells_them_apart_in_targets(env):
    _, prefix, runner = env
    r = runner(
        code_source("qcli-users", [CliMigration("1", "users", create_table(f"{prefix}Users"))]),
        code_source("app", [CliMigration("1", "app", create_table(f"{prefix}App"))]),
    )
    with pytest.raises(CliMigrationError) as err:
        await r.up(Opts(to="1"))
    assert err.value.status == 400
    run = await r.up(Opts(to="qcli-users:1"))
    assert [(a["source"], a["id"]) for a in run.applied] == [("qcli-users", "1")]
    assert [f"{s.source}:{s.id}:{s.status}" for s in await r.status()] == ["qcli-users:1:applied", "app:1:pending"]


async def test_answers_busy_while_a_run_is_in_progress_and_waits_when_told_to(env):
    _, prefix, runner = env
    gate = asyncio.Event()

    async def slow(db):
        await gate.wait()
        await db.exec(create_table(f"{prefix}Slow")[db.dialect])

    r = runner(code_source("app", [CliMigration("1", "slow", slow)]))
    first = asyncio.ensure_future(r.up())
    await asyncio.sleep(0.05)
    with pytest.raises(CliMigrationError) as err:
        await r.up()
    assert err.value.status == 409
    waiting = asyncio.ensure_future(r.up(Opts(wait=True)))
    await asyncio.sleep(0)
    gate.set()
    assert [x["id"] for x in (await first).applied] == ["1"]
    assert (await waiting).applied == []


async def test_adopts_a_table_of_a_previous_release_with_its_data(env):
    connection, prefix, _ = env
    legacy = f"{prefix.lower()}_files"
    table = f"{prefix}Files"
    async with connection.session() as db:
        d = db.dialect
        await db.exec({
            "postgres": f'CREATE TABLE "{legacy}" (id integer PRIMARY KEY, parent_path text)',
            "sqlite": f'CREATE TABLE "{legacy}" (id INTEGER PRIMARY KEY, parent_path TEXT)',
            "mysql": f"CREATE TABLE `{legacy}` (id int PRIMARY KEY, parent_path text)",
            "mssql": f"CREATE TABLE [{legacy}] (id int PRIMARY KEY, parent_path nvarchar(200))",
        }[d])
        await db.query(f"INSERT INTO {q(d, legacy)} (id, parent_path) VALUES (?, ?)", [7, "/docs"])
        assert await adopt_legacy_table(db, legacy, table, {"id": "Id", "parent_path": "ParentPath"})
        assert not await table_exists(db, legacy)
        assert await column_exists(db, table, "ParentPath")
        rows = await db.query(f"SELECT {q(d, 'Id')} AS id, {q(d, 'ParentPath')} AS p FROM {q(d, table)}")
        assert rows == [{"id": 7, "p": "/docs"}]
        assert not await adopt_legacy_table(db, legacy, table, {"id": "Id"})
