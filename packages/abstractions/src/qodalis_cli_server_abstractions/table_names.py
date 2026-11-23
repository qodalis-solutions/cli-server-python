"""Names of the tables CLI plugins create (cli-server-dotnet#99, cli-server-python#83): a common prefix
(``Qcli``) and PascalCase, so they stand apart from an application's own tables in a shared database.  The
same names and options in the Node, .NET and Python servers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: The prefix every plugin table gets unless told otherwise.
DEFAULT_TABLE_PREFIX = "Qcli"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


@dataclass
class CliTableNameOptions:
    """How a plugin store names its tables."""

    #: Prefix of every table (default ``Qcli``; ``""`` gives the plain PascalCase names).
    table_prefix: str | None = None
    #: Per-table names, by the table's base name (``{"Files": "MyFiles"}``); win over the prefix.
    tables: dict[str, str] = field(default_factory=dict)
    #: Schema of the tables, where the database has schemas (PostgreSQL, SQL Server).
    schema: str | None = None
    #: Keep the names of the previous releases (``files``, ...) instead of migrating them.
    legacy_table_names: bool = False


def is_sql_identifier(name: Any) -> bool:
    """True for a name usable as a table, schema or column: letters, digits and ``_``, not starting with a
    digit."""
    return isinstance(name, str) and _IDENTIFIER.match(name) is not None


def qcli_table_name(base: str, options: CliTableNameOptions | None = None, legacy: str | None = None) -> str:
    """The table name for a store's table ``base`` (PascalCase, e.g. ``Files``, ``JobExecutions``): the
    per-table override, else the prefix + base, else (with ``legacy_table_names``) ``legacy``.  Raises
    ``ValueError`` (``invalid table name: <name>``) for a name that is not an identifier, so a configured name
    can never inject SQL."""
    options = options or CliTableNameOptions()
    override = (options.tables or {}).get(base)
    prefix = DEFAULT_TABLE_PREFIX if options.table_prefix is None else options.table_prefix
    if override is not None:
        name = override
    elif options.legacy_table_names and legacy:
        name = legacy
    else:
        name = f"{prefix}{base}"
    if not is_sql_identifier(name):
        raise ValueError(f"invalid table name: {name}")
    if options.schema is not None and not is_sql_identifier(options.schema):
        raise ValueError(f"invalid schema name: {options.schema}")
    return name
