"""The aws plugin's permissions: aws:read on the root ``aws`` command (every sub-command), aws:write on the ones
that change something, both in the permission catalog (group AWS) once the module is added; enforced through
POST /execute on a protected server.  Also ICliModule.permissions in general (add_module)."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli_aws import AWS_PERMISSION_ENTRIES, AwsModule, AwsPermissions

from .command_helpers import add_users, protect, run_command

WRITE = [
    "configure set", "s3 cp", "s3 rm", "s3 mb", "s3 rb", "ec2 start", "ec2 stop", "ec2 reboot",
    "lambda invoke", "sqs send", "sqs purge", "sns publish",
]


def start():
    return create_cli_server(CliServerOptions(configure=lambda b: b.add_module(AwsModule())))


def permissions_by_path(root: Any) -> dict[str, Any]:
    """Every command path under ``aws`` with its own required_permissions."""
    out: dict[str, Any] = {}

    def walk(p: Any, path: list[str]) -> None:
        for child in p.processors or []:
            child_path = [*path, child.command]
            out[" ".join(child_path)] = child.required_permissions
            walk(child, child_path)

    walk(root, [])
    return out


def test_aws_read_on_the_root_aws_write_on_exactly_the_changing_sub_commands():
    result = start()
    root = result.registry.find_processor("aws")
    assert root.required_permissions == [AwsPermissions.READ]
    by_path = permissions_by_path(root)
    for path in WRITE:
        assert by_path[path] == ["aws:write"], path
    others = {path: perms for path, perms in by_path.items() if path not in WRITE}
    assert len(others) > 20
    for path, perms in others.items():
        assert not perms, path


def test_adds_aws_read_and_aws_write_to_the_catalog_once():
    result = start()
    result.builder.add_module(AwsModule())
    entries = [p for p in result.builder.permissions.all() if p.group == "AWS"]
    assert [(p.name, p.surface) for p in entries] == [("aws:read", "cli"), ("aws:write", "cli")]
    assert [e["name"] for e in AWS_PERMISSION_ENTRIES] == ["aws:read", "aws:write"]


def test_a_host_entry_with_the_same_name_stays_as_the_host_registered_it():
    result = create_cli_server()
    result.builder.add_permissions([{"name": "aws:read", "description": "Mine", "group": "Host"}])
    result.builder.add_module(AwsModule())
    mine = result.builder.permissions.find("aws:read")
    assert (mine.description, mine.group, mine.surface) == ("Mine", "Host", "host")
    write = result.builder.permissions.find("aws:write")
    assert (write.group, write.surface) == ("AWS", "cli")


def test_protected_server_aws_read_to_read_aws_write_as_well_to_change():
    result = start()
    protect(result)
    with TestClient(result.app) as client:
        tokens = add_users(client, {
            "runner": ["commands:execute"],
            "reader": ["commands:execute", "aws:read"],
            "writer": ["commands:execute", "aws:read", "aws:write"],
            "writeonly": ["commands:execute", "aws:write"],
        })

        def run(line: str, user: str):
            return run_command(client, result, line, tokens[user])

        no_read = run("aws configure get", "runner")
        assert no_read.status == 403
        assert "aws:read" in json.dumps(no_read.body)
        assert run("aws configure get", "reader").status == 200

        no_write = run("aws configure set --region eu-north-1", "reader")
        assert no_write.status == 403
        assert "aws:write" in json.dumps(no_write.body)
        assert run("aws s3 rm s3://bucket/key", "reader").status == 403
        assert run("aws ec2 stop i-123", "reader").status == 403

        # aws:write alone is not enough: the root needs aws:read
        assert run("aws configure set --region eu-north-1", "writeonly").status == 403

        ok = run("aws configure set --region eu-north-1", "writer")
        assert ok.status == 200
        assert "AWS configuration updated." in ok.text
        assert "eu-north-1" in run("aws configure get", "writer").text
