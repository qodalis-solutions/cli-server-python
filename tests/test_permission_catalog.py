"""The permission catalog: core entries, host and plugin registration, validation."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qodalis_cli import (  # noqa: E402
    CORE_PERMISSIONS,
    CliPermission,
    CliPermissionCatalog,
    CliServerOptions,
    create_cli_server,
)
from qodalis_cli.extensions import CliBuilder  # noqa: E402
from qodalis_cli.services import CliCommandRegistry  # noqa: E402

CORE_NAMES = [
    "commands:execute",
    "shell:execute",
    "http:request",
    "files:read",
    "files:write",
    "data:query",
    "jobs:read",
    "jobs:manage",
    "logs:read",
    "admin:access",
    "config:read",
    "config:write",
    "audit:read",
    "audit:clear",
]

RATES = CliPermission(name="rates:read", description="Official exchange rates", group="Rates")


def _builder() -> CliBuilder:
    return CliBuilder(CliCommandRegistry())


def test_core_permissions_seed_the_catalog_in_order():
    catalog = _builder().permissions
    assert [p.name for p in catalog.all()] == CORE_NAMES
    assert all(p.surface == "cli" for p in catalog.all())
    assert [p.name for p in CORE_PERMISSIONS] == CORE_NAMES


def test_core_permissions_text():
    by_name = {p.name: p for p in CORE_PERMISSIONS}
    assert (by_name["commands:execute"].group, by_name["commands:execute"].description) == (
        "Commands",
        "Run commands on the server",
    )
    assert by_name["shell:execute"].description == "Open an interactive shell on the server"
    assert (by_name["http:request"].group, by_name["http:request"].description) == (
        "Commands",
        "Make HTTP requests from the server",
    )
    assert by_name["files:write"].description == "Upload, create and delete files"
    assert (by_name["data:query"].group, by_name["data:query"].description) == (
        "Data",
        "Query the data explorer's sources",
    )
    assert by_name["jobs:read"].description == "See jobs and their runs"
    assert by_name["jobs:manage"].description == "Start, stop, pause and change jobs"
    assert (by_name["logs:read"].group, by_name["logs:read"].description) == (
        "Server",
        "Read the server's logs",
    )
    assert by_name["admin:access"].description == "Open the admin dashboard and its server pages"
    assert (by_name["audit:read"].group, by_name["audit:read"].description) == (
        "Audit",
        "Read the audit log",
    )
    assert by_name["audit:clear"].description == "Clear the audit log"


def test_to_dict_shape():
    assert CORE_PERMISSIONS[3].to_dict() == {
        "name": "files:read",
        "description": "List, read and download files",
        "group": "Files",
        "surface": "cli",
    }
    assert list(CORE_PERMISSIONS[3].to_dict()) == ["name", "description", "group", "surface"]


def test_host_entries_through_create_cli_server():
    result = create_cli_server(
        CliServerOptions(configure=lambda b: b.add_permissions([RATES]))
    )
    entries = result.builder.permissions.all()
    assert [p.name for p in entries] == [*CORE_NAMES, "rates:read"]
    assert entries[-1] == CliPermission(
        name="rates:read",
        description="Official exchange rates",
        group="Rates",
        surface="host",
    )


def test_plain_dict_entries():
    builder = _builder()
    returned = builder.add_permissions(
        [
            {"name": "rates:read", "description": "Official exchange rates", "group": "Rates"},
            {"name": "banks:read", "description": "Banks and their buy and sell rates", "group": "Rates"},
        ]
    )
    assert returned is builder
    entries = builder.permissions.all()[len(CORE_NAMES):]
    assert [(p.name, p.surface) for p in entries] == [("rates:read", "host"), ("banks:read", "host")]


def test_surface_cli():
    builder = _builder()
    builder.add_permissions(
        [{"name": "weather:read", "description": "Read the weather", "group": "Weather"}],
        surface="cli",
    )
    assert builder.permissions.find("weather:read").surface == "cli"


def test_surface_argument_overrides_the_entry():
    builder = _builder()
    builder.add_permissions(
        [CliPermission(name="rates:read", description="Rates", group="Rates", surface="cli")]
    )
    assert builder.permissions.find("rates:read").surface == "host"


def test_find_is_case_insensitive():
    builder = _builder()
    builder.add_permissions([RATES])
    assert builder.permissions.find("RATES:Read").name == "rates:read"
    assert builder.permissions.find("files:READ").name == "files:read"
    assert builder.permissions.find("rates:*") is None
    assert builder.permissions.find("nope:read") is None


def test_host_entries_follow_plugin_entries():
    """Host entries come after core and plugin (``cli``) entries, although a
    Python host registers in ``configure`` before plugins are mounted."""
    builder = _builder()
    builder.add_permissions([RATES])
    builder.add_permissions(
        [{"name": "weather:read", "description": "Read the weather", "group": "Weather"}],
        surface="cli",
    )
    assert [p.name for p in builder.permissions.all()][-2:] == ["weather:read", "rates:read"]


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "RATES:READ", "description": "d", "group": "g"},
        {"name": "rates", "description": "d", "group": "g"},
        {"name": "rates:*", "description": "d", "group": "g"},
        {"name": "*", "description": "d", "group": "g"},
        {"name": "rates:read:all", "description": "d", "group": "g"},
        {"name": "rates read", "description": "d", "group": "g"},
        {"name": ":read", "description": "d", "group": "g"},
        {"name": "", "description": "d", "group": "g"},
        {"name": "rates:read", "description": "", "group": "g"},
        {"name": "rates:read", "description": "x" * 201, "group": "g"},
        {"name": "rates:read", "description": "two\nlines", "group": "g"},
        {"name": "rates:read", "description": "d", "group": ""},
        {"name": "rates:read", "description": "d", "group": "g" * 65},
        {"name": "rates:read", "description": "d"},
        {"name": 1, "description": "d", "group": "g"},
    ],
)
def test_invalid_entries_throw(entry):
    with pytest.raises(ValueError) as error:
        _builder().add_permissions([entry])
    assert str(error.value) == f"invalid permission entry: {entry.get('name')}"


def test_limits_are_inclusive():
    builder = _builder()
    builder.add_permissions(
        [{"name": "a_1.b-c:d_2.e-f", "description": "x" * 200, "group": "g" * 64}]
    )
    assert builder.permissions.find("a_1.b-c:d_2.e-f") is not None


def test_invalid_surface_throws():
    with pytest.raises(ValueError) as error:
        _builder().add_permissions([RATES], surface="api")  # type: ignore[arg-type]
    assert str(error.value) == "invalid permission entry: rates:read"


def test_duplicate_throws():
    builder = _builder()
    with pytest.raises(ValueError) as error:
        builder.add_permissions(
            [{"name": "audit:read", "description": "Audit", "group": "Audit"}]
        )
    assert str(error.value) == "permission already registered: audit:read"

    builder.add_permissions([RATES])
    with pytest.raises(ValueError) as error:
        builder.add_permissions([RATES])
    assert str(error.value) == "permission already registered: rates:read"


def test_duplicate_is_case_insensitive_and_checked_before_syntax():
    builder = _builder()
    with pytest.raises(ValueError) as error:
        builder.add_permissions([{"name": "FILES:READ", "description": "Files", "group": "Files"}])
    assert str(error.value) == "permission already registered: FILES:READ"


def test_duplicate_inside_one_call_registers_nothing():
    builder = _builder()
    with pytest.raises(ValueError):
        builder.add_permissions(
            [
                {"name": "banks:read", "description": "Banks", "group": "Rates"},
                {"name": "banks:read", "description": "Banks again", "group": "Rates"},
            ]
        )
    assert builder.permissions.find("banks:read") is None


def test_duplicate_fails_startup():
    with pytest.raises(ValueError, match="permission already registered: files:read"):
        create_cli_server(
            CliServerOptions(
                configure=lambda b: b.add_permissions(
                    [{"name": "files:read", "description": "Files", "group": "Files"}]
                )
            )
        )


def test_catalog_is_standalone():
    catalog = CliPermissionCatalog()
    assert catalog.all() == []
    catalog.add([RATES], "host")
    assert catalog.all() == [RATES]
    # all() is a copy
    catalog.all().clear()
    assert len(catalog.all()) == 1
