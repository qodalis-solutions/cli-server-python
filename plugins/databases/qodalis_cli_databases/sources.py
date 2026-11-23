"""Where a database's migrations come from: a folder of SQL files, or code (how plugins ship their schema)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence, Union

from .connection import CliSqlSession

#: A migration step: SQL, SQL per dialect (``{"postgres": ..., "sqlite": ...}``), or a coroutine function
#: given the session.
CliMigrationStep = Union[str, Mapping[str, str], Callable[[CliSqlSession], Awaitable[None]]]


@dataclass(frozen=True)
class CliMigration:
    """One schema change.  Released migrations are never edited or reordered: add a new one instead."""

    #: Unique within its source, sortable: ``0001``, ``2``, ``20261120111649``.
    id: str
    #: Shown in the dashboard: ``create users``.
    name: str
    up: CliMigrationStep
    #: Undoes ``up``; without it the migration cannot be rolled back.
    down: CliMigrationStep | None = None
    #: Run inside a transaction with its history row (False for ``CREATE INDEX CONCURRENTLY``).
    transaction: bool = True


class CliMigrationSource(Protocol):
    """A source of migrations: ``name`` is unique per database (``app``, ``qcli-users``) and stored with every
    applied migration; ``load`` returns the migrations in any order (the runner sorts them by id)."""

    name: str

    async def load(self) -> list[CliMigration]: ...


_NUMBER = re.compile(r"(\d+)")


def id_sort_key(value: str) -> list[Any]:
    """Ids compared as numbers where they are numbers (``2`` before ``10``), else as text."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.lower()) for part in _NUMBER.split(value) if part]


def step_sql(step: CliMigrationStep, dialect: str, migration: str) -> str | None:
    """The SQL a step runs on ``dialect``, or None for a code step; raises when the step has none for the
    dialect."""
    if callable(step):
        return None
    if isinstance(step, str):
        return step
    sql = step.get(dialect)
    if sql is None:
        raise ValueError(f"migration {migration} has no SQL for {dialect}")
    return sql


def checksum_of(migration: CliMigration, dialect: str) -> str | None:
    """SHA-256 of the up step's SQL for the dialect; None for code (it cannot be compared reliably)."""
    sql = step_sql(migration.up, dialect, migration.id)
    return None if sql is None else hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


_NO_TRANSACTION = re.compile(r"^\s*--\s*qcli:no-transaction\b", re.IGNORECASE)
_FILE = re.compile(r"^([0-9A-Za-z]+)_(.+?)\.(up|down)\.sql$")


class SqlFolderSource:
    """Migrations from a folder: ``NNNN_name.up.sql``, with an optional ``NNNN_name.down.sql``.  A file
    starting with ``-- qcli:no-transaction`` runs outside a transaction.  Read again on every load, so a new
    file shows as pending."""

    def __init__(self, directory: str, name: str = "app") -> None:
        self.directory = directory
        self.name = name

    async def load(self) -> list[CliMigration]:
        by_id: dict[str, dict[str, str]] = {}
        for file in sorted(os.listdir(self.directory)):
            match = _FILE.match(file)
            if not match:
                continue
            mid, title, direction = match.groups()
            title = re.sub(r"[_-]+", " ", title)
            entry = by_id.setdefault(mid, {"name": title})
            if entry["name"] != title:
                raise ValueError(f"migration {mid} has files with different names in {self.directory}")
            with open(os.path.join(self.directory, file), encoding="utf-8") as f:
                entry[direction] = f.read()
        out: list[CliMigration] = []
        for mid, entry in by_id.items():
            if "up" not in entry:
                raise ValueError(f"migration {mid} has a down file but no up file in {self.directory}")
            out.append(CliMigration(
                id=mid,
                name=entry["name"],
                up=entry["up"],
                down=entry.get("down"),
                transaction=not _NO_TRANSACTION.match(entry["up"]),
            ))
        return out


class CodeSource:
    """Migrations given in code (how plugins ship their schema)."""

    def __init__(self, name: str, migrations: Sequence[CliMigration]) -> None:
        self.name = name
        self._migrations = list(migrations)

    async def load(self) -> list[CliMigration]:
        return [replace(m, id=str(m.id)) for m in self._migrations]


def sql_folder_source(directory: str, name: str = "app") -> SqlFolderSource:
    """Migrations from ``NNNN_name.up.sql`` / ``.down.sql`` files in ``directory``."""
    return SqlFolderSource(directory, name)


def code_source(name: str, migrations: Sequence[CliMigration]) -> CodeSource:
    """Migrations given in code, as source ``name``."""
    return CodeSource(name, migrations)
