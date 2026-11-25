"""Shared helpers for terminal-command tests: run a command line through POST /api/v1/qcli/execute the way the web
terminal sends it, and protect a server with users holding chosen permissions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from qodalis_cli_server_abstractions import command_matches, split_shell_words

V1 = "/api/v1/qcli"
ROOT_PASS = "rootpass1"
_FLAG = re.compile(r"^--?([A-Za-z][\w-]*)$")


@dataclass
class CommandResult:
    status: int
    #: The text outputs joined by new lines.
    text: str
    exit_code: Any
    body: Any


def run_command(client: TestClient, result: Any, line: str, token: str | None = None) -> CommandResult:
    """Runs a command line as the browser sends it: the sub-commands the registry knows become ``chainCommands``, the
    other words positionals (and ``value``), ``--name value`` / ``-n value`` args, a lone ``--flag`` true."""
    command, *words = split_shell_words(line)
    chain: list[str] = []
    positionals: list[str] = []
    args: dict[str, Any] = {}
    processor = result.registry.find_processor(command)
    i = 0
    while i < len(words):
        w = words[i]
        flag = _FLAG.match(w)
        if flag:
            nxt = words[i + 1] if i + 1 < len(words) else None
            if nxt is not None and not re.match(r"^--?[A-Za-z]", nxt):
                args[flag.group(1)] = nxt
                i += 2
            else:
                args[flag.group(1)] = True
                i += 1
            continue
        sub = None
        if not positionals and processor is not None:
            sub = next((p for p in (processor.processors or []) if command_matches(p, w)), None)
        if sub is not None:
            chain.append(w)
            processor = sub
        else:
            positionals.append(w)
        i += 1
    body: dict[str, Any] = {
        "command": command, "chainCommands": chain, "rawCommand": line, "positionals": positionals, "args": args,
    }
    if positionals:
        body["value"] = " ".join(positionals)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    res = client.post(f"{V1}/execute", headers=headers, json=body)
    try:
        data = res.json()
    except ValueError:
        data = None
    outputs = data.get("outputs") if isinstance(data, dict) else None
    text = "\n".join(
        o["value"] if isinstance(o.get("value"), str) else json.dumps(o["value"] if "value" in o else o)
        for o in outputs or []
    )
    return CommandResult(res.status_code, text, data.get("exitCode") if isinstance(data, dict) else None, data)


def sub_permissions(result: Any, command: str) -> dict[str, Any]:
    """The sub-commands of a registered command and their permissions, ``{"list": ["x:read"], ...}``."""
    processor = result.registry.find_processor(command)
    return {p.command: p.required_permissions for p in processor.processors or []}


def protect(result: Any) -> Any:
    """Mounts the users plugin (root's password :data:`ROOT_PASS`); returns it.  Create users with :func:`add_users`
    once a ``TestClient`` runs the app."""
    from qodalis_cli_users import CliUsersBuilder

    users = CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(10_000).set_min_password_length(4).build()
    result.mount_plugin(users)
    return users


def login(client: TestClient, username: str, password: str) -> str:
    res = client.post(f"{V1}/sessions", json={"username": username, "password": password})
    assert res.status_code == 201, res.text
    return res.json()["token"]


def add_users(client: TestClient, users: dict[str, list[str]]) -> dict[str, str]:
    """Creates one user per entry, each in its own group with those permissions; a session token per user name (and
    ``root``)."""
    tokens = {"root": login(client, "root", ROOT_PASS)}
    root = {"Authorization": f"Bearer {tokens['root']}"}
    for name, permissions in users.items():
        res = client.post(f"{V1}/groups", headers=root, json={"name": f"{name}s", "permissions": permissions})
        assert res.status_code == 201, res.text
        res = client.post(f"{V1}/users", headers=root, json={
            "name": name, "email": f"{name}@x.io", "password": f"{name}pass", "groups": [f"{name}s"],
        })
        assert res.status_code == 201, res.text
        tokens[name] = login(client, name, f"{name}pass")
    return tokens
