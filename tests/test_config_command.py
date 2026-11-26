"""The ``config`` terminal command through POST /api/v1/qcli/execute: list,
get, set, reset; values parsed by type, per-key errors, --expected-version
conflicts, secrets never printed, config:read / config:write on a protected
server.  Port of the Node ``config-command.test.ts``."""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qodalis_cli import CliServerOptions, create_cli_server
from qodalis_cli.config import format_config_value
from qodalis_cli.create_cli_server import ANONYMOUS_ADMIN_WARNING, CliServerConfigOptions, log_startup_warning
from qodalis_cli_server_abstractions.audit import AuditQueryOptions
from qodalis_cli_server_abstractions.config import (
    CliConfigSectionDefinition,
    CliConfigSettingDefinition,
    CliConfigValidation,
    CliStoredConfigProvider,
    InMemoryConfigStore,
    JsonFileConfigStore,
    cli_config_context,
)
from qodalis_cli_server_abstractions.shell_words import split_shell_words
from qodalis_cli_admin import AdminBuildDeps, CliAdminBuilder
from qodalis_cli_users import CliUsersBuilder

V1 = "/api/v1/qcli"
ROOT_PASS = "rootpass1"


def app_provider() -> CliStoredConfigProvider:
    return CliStoredConfigProvider(
        id="app",
        title="App",
        description="The app",
        sections=[
            CliConfigSectionDefinition(id="main", title="Main", settings=[
                CliConfigSettingDefinition(
                    key="limit", type="number", default=5, validation=CliConfigValidation(min=1, max=10),
                    description="How many",
                ),
                CliConfigSettingDefinition(key="apiKey", type="secret"),
                CliConfigSettingDefinition(key="tags", type="string[]", default=[]),
                CliConfigSettingDefinition(key="beta", type="boolean", default=False),
                CliConfigSettingDefinition(key="timeout", type="duration", default="30s", requires_restart=True),
                CliConfigSettingDefinition(
                    key="mode", type="enum", default="a", validation=CliConfigValidation(options=["a", "b"]),
                ),
                CliConfigSettingDefinition(key="fixed", type="string", mutable=False, default="x"),
            ]),
            # A section id with a dot: the longest matching section wins
            CliConfigSectionDefinition(id="main.extra", settings=[
                CliConfigSettingDefinition(key="deep.key", type="string", default="d"),
            ]),
        ],
    )


class Runner:
    def __init__(self, client: TestClient, result: Any) -> None:
        self.c, self.result = client, result

    def run(self, line: str, token: str | None = None) -> tuple[int, str, Any]:
        """Runs a command line the way the browser sends it (sub-commands, positionals, --flags)."""
        words = split_shell_words(line)
        command, sub, rest = words[0], (words[1] if len(words) > 1 else None), words[2:]
        positionals: list[str] = []
        args: dict[str, Any] = {}
        i = 0
        while i < len(rest):
            if rest[i].startswith("--"):
                args[rest[i][2:]] = rest[i + 1] if i + 1 < len(rest) else None
                i += 2
            else:
                positionals.append(rest[i])
                i += 1
        body: dict[str, Any] = {
            "command": command,
            "chainCommands": [sub] if sub else [],
            "rawCommand": line,
            "positionals": positionals,
            "args": args,
        }
        if positionals:
            body["value"] = " ".join(positionals)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        res = self.c.post(f"{V1}/execute", headers=headers, json=body)
        try:
            data = res.json()
        except ValueError:
            data = None
        outputs = data.get("outputs") if isinstance(data, dict) else None
        text = "\n".join(str(o.get("value")) for o in outputs or [])
        return res.status_code, text, data

    def text(self, line: str, token: str | None = None) -> str:
        return self.run(line, token)[1]


# ---------------------------------------------------------------------------
# Open server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cli():
    result = create_cli_server(CliServerOptions(
        configure=lambda b: b.add_config_provider(app_provider()), allow_anonymous_admin=True,
    ))
    with TestClient(result.app) as client:
        yield Runner(client, result)


def test_an_open_server_without_allow_anonymous_admin_refuses_set_and_reset_and_still_reads():
    result = create_cli_server(CliServerOptions(configure=lambda b: b.add_config_provider(app_provider())))
    with TestClient(result.app) as client:
        cli = Runner(client, result)
        refusal = "Error: sign-in required: protect the server (users plugin) or set allow_anonymous_admin=True (config:write)"
        assert cli.text("config set app.main.limit 7") == refusal
        assert cli.text("config reset app.main.limit") == refusal
        assert "5" in cli.text("config get app.main.limit")


class TestConfigCommandOpenServer:
    def test_is_registered_with_its_sub_commands_and_permissions(self, cli: Runner):
        processor = cli.result.registry.find_processor("config")
        assert processor is not None
        assert list(processor.required_permissions) == ["config:read"]
        subs = {p.command: list(p.required_permissions) for p in processor.processors or []}
        assert subs == {
            "list": ["config:read"], "get": ["config:read"], "set": ["config:write"], "reset": ["config:write"],
        }

    def test_config_and_config_list_providers_and_sections(self, cli: Runner):
        text = cli.text("config list")
        assert text.split("\n")[0] == "Default store: In memory, changes are lost on restart"
        assert "server  Server  (read-only)" in text
        assert "host  Host  (read-only; from Server options, Environment variables)" in text
        assert re.search(
            r"app {2}App {2}· In memory\n {2}main +7 setting\(s\) {2}version 0\n {2}main\.extra +1 setting\(s\) {2}version 0", text,
        ), text
        assert "custom  Custom  · In memory" in text
        assert cli.text("config") == text

    def test_get_provider_section_and_key(self, cli: Runner):
        provider = cli.text("config get app")
        assert "app  App  The app" in provider
        assert "app.main  Main  (version 0)" in provider
        assert re.search(r"limit +5 {2}\[number, default\]", provider), provider
        assert re.search(r"apiKey +\(not set\) {2}\[secret, default\]", provider), provider
        assert re.search(r"fixed +x {2}\[string, default, read-only\]", provider), provider
        assert re.search(r"timeout +30s {2}\[duration, default, restart\]", provider), provider

        section = cli.text("config get app.main.extra")
        assert "app.main.extra" in section
        assert "deep.key" in section

        assert cli.text("config get app.main.limit") == "\n".join([
            "app.main.limit",
            "  Label         limit",
            "  Description   How many",
            "  Type          number",
            "  Value         5",
            "  Default       5",
            "  Source        default",
            "  Mutable       yes",
            "  Validation    min 1; max 10",
            "  Version       0",
        ])
        assert "Value         d" in cli.text("config get app.main.extra.deep.key")

    def test_get_errors_for_unknown_providers_sections_and_keys(self, cli: Runner):
        assert cli.text("config get nope") == "Error: unknown config provider: nope"
        assert cli.text("config get app.nope") == "Error: unknown config section: app.nope"
        assert cli.text("config get app.main.nope") == "Error: unknown setting: app.main.nope"

    def test_set_values_parsed_by_type(self, cli: Runner):
        assert cli.text("config set app.main.limit 7") == "Updated app.main.limit = 7 (version 1)"
        assert cli.text("config set app.main.tags a,b, c") == 'Updated app.main.tags = ["a","b","c"] (version 2)'
        assert cli.text("config set app.main.beta yes") == "Updated app.main.beta = true (version 3)"
        assert cli.text("config set app.main.mode b") == "Updated app.main.mode = b (version 4)"
        assert 'Value         ["a","b","c"]' in cli.text("config get app.main.tags")

    def test_set_a_change_that_needs_a_restart_says_so(self, cli: Runner):
        text = cli.text("config set app.main.timeout 1m")
        assert re.fullmatch(
            r"Updated app\.main\.timeout = 1m \(version \d+\)\nRestart the server for the change to take effect\.", text,
        ), text

    def test_set_validation_parse_read_only_and_unknown_errors(self, cli: Runner):
        assert cli.text("config set app.main.limit 50") == "Error: validation failed\n  limit: must be at most 10"
        assert cli.text("config set app.main.limit lots") == "Error: validation failed\n  limit: must be a number"
        assert cli.text("config set app.main.beta maybe") == "Error: validation failed\n  beta: must be true or false"
        assert cli.text("config set app.main.mode z") == "Error: validation failed\n  mode: must be one of: a, b"
        assert cli.text("config set app.main.fixed y") == "Error: validation failed\n  fixed: read-only"
        assert cli.text("config set app.main.ghost 1") == "Error: validation failed\n  ghost: unknown setting"
        assert cli.text("config set server.runtime.pid 1") == "Error: config provider server is read-only"
        assert cli.text("config set app.main 1") == "Error: name a setting: <provider>.<section>.<key>"
        assert "usage: config set" in cli.text("config set app.main.limit")

    def test_set_expected_version_refused_when_stale(self, cli: Runner):
        view = cli.c.portal.call(cli.result.config.get, "app", cli_config_context(None, "test", lambda _p: True))
        current = view["sections"][0]["version"]
        assert current is not None
        assert cli.text("config set app.main.limit 3 --expected-version 0") == (
            f"Error: version conflict (current version {current})"
        )
        assert cli.text("config set app.main.limit 3 --version 0") == f"Error: version conflict (current version {current})"
        assert cli.text(f"config set app.main.limit 3 --expected-version {current}") == (
            f"Updated app.main.limit = 3 (version {int(current) + 1})"
        )

    def test_secrets_are_accepted_never_printed(self, cli: Runner):
        set_text = cli.text('config set app.main.apiKey "sk_cmd_secret value"')
        assert re.fullmatch(r"Updated app\.main\.apiKey = \(set\) \(version \d+\)", set_text), set_text
        everything = "\n".join([cli.text("config get app"), cli.text("config get app.main.apiKey"), cli.text("config list")])
        assert "sk_cmd_secret" not in everything
        entry = cli.text("config get app.main.apiKey")
        assert "Value         (set)" in entry
        assert "Default" not in entry
        assert re.match(r"Reset app\.main\.apiKey = \(not set\)", cli.text("config reset app.main.apiKey"))

    def test_reset_back_to_the_default_free_keys_removed(self, cli: Runner):
        cli.text("config set app.main.limit 9")
        assert re.fullmatch(r"Reset app\.main\.limit = 5 \(version \d+\)", cli.text("config reset app.main.limit"))
        assert cli.text("config set custom.custom.motd Hello there") == "Updated custom.custom.motd = Hello there (version 1)"
        assert cli.text("config set custom.custom.retries 3") == "Updated custom.custom.retries = 3 (version 2)"
        assert "Type          number" in cli.text("config get custom.custom.retries")
        assert cli.text("config reset custom.custom.motd") == "Reset custom.custom.motd (version 3)"
        assert cli.text("config reset app.main.limit --expected-version 0").startswith("Error: version conflict")
        assert "usage: config reset" in cli.text("config reset")

    def test_format_config_value(self):
        assert format_config_value({"value": "x", "sensitive": False}) == "x"
        assert format_config_value({"value": None, "sensitive": False}) == "(none)"
        assert format_config_value({"value": {"a": 1}, "sensitive": False}) == '{"a":1}'
        assert format_config_value({"value": None, "sensitive": True, "isSet": True}) == "(set)"

    def test_can_be_left_out_with_config_command_false(self):
        result = create_cli_server(CliServerOptions(config=CliServerConfigOptions(command=False)))
        assert result.registry.find_processor("config") is None


class TestConfigListStore:
    def test_a_json_file_outside_the_app_its_name_only_kept_across_restarts(self, tmp_path):
        store = JsonFileConfigStore(tmp_path / "qcli-demo-config.json")
        result = create_cli_server(CliServerOptions(config=CliServerConfigOptions(store=store)))
        with TestClient(result.app) as client:
            text = Runner(client, result).text("config list")
        assert text.split("\n")[0] == "Default store: JSON file (qcli-demo-config.json), kept across restarts"
        assert str(tmp_path) not in text

    def test_a_store_that_does_not_describe_itself_a_custom_store_named_after_its_class(self):
        class VaultStore(InMemoryConfigStore):
            source = "database"

        store = VaultStore()
        store.describe = None  # type: ignore[assignment,method-assign]
        result = create_cli_server(CliServerOptions(config=CliServerConfigOptions(store=store)))
        with TestClient(result.app) as client:
            text = Runner(client, result).text("config list")
        assert text.split("\n")[0] == "Default store: Custom store (VaultStore), kept across restarts"


# ---------------------------------------------------------------------------
# Protected server (users plugin)
# ---------------------------------------------------------------------------


class _Protected(Runner):
    def __init__(self, client: TestClient, result: Any, users: Any, tokens: dict[str, str]) -> None:
        super().__init__(client, result)
        self.users, self.tokens = users, tokens


@pytest.fixture(scope="module")
def protected():
    result = create_cli_server(CliServerOptions(configure=lambda b: b.add_config_provider(app_provider())))
    users = (
        CliUsersBuilder().set_default_admin_password(ROOT_PASS).set_rate_limit(10_000).set_min_password_length(4).build()
    )
    result.mount_plugin(users)
    tokens: dict[str, str] = {}
    with TestClient(result.app) as client:
        def login(username: str, password: str) -> str:
            res = client.post(f"{V1}/sessions", json={"username": username, "password": password})
            assert res.status_code == 201, res.text
            return res.json()["token"]

        tokens["root"] = login("root", ROOT_PASS)
        root = {"Authorization": f"Bearer {tokens['root']}"}

        def mk(name: str, permissions: list[str]) -> None:
            res = client.post(f"{V1}/groups", headers=root, json={"name": f"{name}s", "permissions": permissions})
            assert res.status_code == 201, res.text
            res = client.post(f"{V1}/users", headers=root, json={
                "name": name, "email": f"{name}@x.io", "password": f"{name}pass", "groups": [f"{name}s"],
            })
            assert res.status_code == 201, res.text
            tokens[name] = login(name, f"{name}pass")

        mk("runner", ["commands:execute"])
        mk("reader", ["commands:execute", "config:read"])
        mk("writer", ["commands:execute", "config:read", "config:write"])
        yield _Protected(client, result, users, tokens)


class TestConfigCommandProtectedServer:
    def test_config_read_for_list_and_get(self, protected: _Protected):
        assert protected.run("config list", protected.tokens["runner"])[0] == 403
        status, text, _ = protected.run("config get app.main.limit", protected.tokens["reader"])
        assert status == 200
        assert "Value         5" in text

    def test_config_write_for_set_and_reset(self, protected: _Protected):
        status, _, body = protected.run("config set app.main.limit 2", protected.tokens["reader"])
        assert status == 403
        assert "config:write" in body["error"]
        assert protected.run("config reset app.main.limit", protected.tokens["reader"])[0] == 403
        assert protected.text("config set app.main.limit 2", protected.tokens["writer"]) == (
            "Updated app.main.limit = 2 (version 1)"
        )

    def test_changes_are_audited_with_via_terminal_and_the_signed_in_user(self, protected: _Protected):
        protected.text("config set app.main.apiKey terminal-secret", protected.tokens["writer"])
        entries = protected.c.portal.call(
            protected.users.audit_store.get_entries, AuditQueryOptions(event="config.update"),
        )
        entry = next(e for e in entries if e.properties["changes"][0]["key"] == "apiKey")
        assert entry.username == "writer"
        assert entry.target == "config:app.main"
        assert entry.properties["via"] == "terminal"
        assert entry.properties["changes"] == [{"key": "apiKey", "old": None, "new": "***"}]
        assert "terminal-secret" not in json.dumps([vars(e) for e in entries], default=str)


def test_allow_anonymous_admin_logs_a_warning_at_startup(caplog):
    with caplog.at_level("WARNING", logger="qodalis_cli.create_cli_server"):
        with TestClient(create_cli_server(CliServerOptions(allow_anonymous_admin=True)).app):
            pass
    assert any("allow_anonymous_admin is on" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING", logger="qodalis_cli.create_cli_server"):
        with TestClient(create_cli_server().app):
            pass
    assert not any("allow_anonymous_admin" in r.getMessage() for r in caplog.records)


def _isolated_logger(name: str) -> logging.Logger:
    target = logging.getLogger(name)
    target.handlers.clear()
    target.propagate = False
    target.setLevel(logging.WARNING)
    return target


def test_startup_warning_goes_to_stderr_when_logging_is_not_configured(capsys):
    target = _isolated_logger("qcli.test.startup-warning.bare")
    ring: list[str] = []

    class _Buffer(logging.Handler):  # not a stream: as the log socket and the admin log buffer
        def emit(self, record: logging.LogRecord) -> None:
            ring.append(record.getMessage())

    target.addHandler(_Buffer())
    log_startup_warning(ANONYMOUS_ADMIN_WARNING, target)
    assert ANONYMOUS_ADMIN_WARNING in capsys.readouterr().err
    assert ring == [ANONYMOUS_ADMIN_WARNING]


def test_startup_warning_not_printed_twice_when_a_console_handler_writes_it(capsys):
    target = _isolated_logger("qcli.test.startup-warning.configured")
    stream = io.StringIO()
    target.addHandler(logging.StreamHandler(stream))
    log_startup_warning(ANONYMOUS_ADMIN_WARNING, target)
    assert ANONYMOUS_ADMIN_WARNING not in capsys.readouterr().err
    assert ANONYMOUS_ADMIN_WARNING in stream.getvalue()


def test_allow_anonymous_admin_warning_lands_in_the_admin_log_buffer():
    result = create_cli_server(CliServerOptions(allow_anonymous_admin=True))
    admin = CliAdminBuilder().set_credentials("admin", "adminpass").set_jwt_secret("secret-for-the-warning-test").build(
        AdminBuildDeps(registry=result.registry, event_socket_manager=result.event_socket_manager, builder=result.builder),
    )
    result.mount_plugin(admin)
    try:
        with TestClient(result.app):
            pass
        entries, _ = admin.log_buffer.query(search="allow_anonymous_admin is on")
        assert entries and entries[0]["level"] in ("WARN", "WARNING")
    finally:
        admin.log_buffer.uninstall_handler()
