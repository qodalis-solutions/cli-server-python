"""Remote shell security (Appendix C) without the users plugin: modes,
locked default, close codes, limits, timeouts, minimal environment."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qodalis_cli import CliServerOptions, CliShellOptions, create_cli_server
from qodalis_cli import shell_security

SHELL = "/ws/qcli/shell"
SECRET = "s3cret-token"
BEARER = {"Authorization": f"Bearer {SECRET}"}


def _server(enable_shell: bool = True, **shell: Any):
    shell.setdefault("shell", "/bin/sh")
    return create_cli_server(CliServerOptions(enable_shell=enable_shell, shell=CliShellOptions(**shell)))


def _rejected(client: TestClient, url: str, headers: dict | None = None) -> WebSocketDisconnect:
    with client.websocket_connect(url, headers=headers or {}) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    return exc.value


def _run(client: TestClient, url: str, headers: dict | None = None) -> tuple[str, int | None]:
    """Run a one-shot command; returns (stdout, exit code)."""
    stdout, code = "", None
    with client.websocket_connect(url, headers=headers or {}) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        for _ in range(500):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "stdout":
                stdout += msg["data"]
            elif msg["type"] == "exit":
                code = msg["code"]
                break
    return stdout, code


def _until_closed(ws) -> tuple[list[dict], WebSocketDisconnect]:
    messages = []
    while True:
        try:
            messages.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect as exc:
            return messages, exc


def _shell_pid(ws) -> int:
    """Ask an interactive shell for its pid."""
    assert json.loads(ws.receive_text())["type"] == "ready"
    ws.send_text(json.dumps({"type": "stdin", "data": "echo PID=$$\n"}))
    out = ""
    for _ in range(200):
        msg = json.loads(ws.receive_text())
        if msg["type"] == "stdout":
            out += msg["data"]
            # The prompt may precede the output ("# PID=123") when the shell
            # starts after the terminal already echoed the command
            match = re.search(r"PID=(\d+)", out)
            if match:
                return int(match.group(1))
    raise AssertionError(f"no pid in {out!r}")


def _gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# C1 — enabling and modes
# ---------------------------------------------------------------------------

class TestModes:
    def test_disabled_closes_4404(self):
        result = _server(enable_shell=False)
        with TestClient(result.app) as c:
            caps = c.get("/api/qcli/capabilities").json()
            assert caps["shell"] is False
            assert "shellAuth" not in caps
            exc = _rejected(c, SHELL)
            assert (exc.code, exc.reason) == (4404, "shell disabled")
            assert _rejected(c, "/ws/v1/qcli/shell").code == 4404

    def test_enabled_without_mode_is_locked(self, caplog):
        result = create_cli_server(CliServerOptions(enable_shell=True))
        with caplog.at_level(logging.INFO):
            with TestClient(result.app) as c:
                for path in ("/api/qcli/capabilities", "/api/v1/qcli/capabilities"):
                    caps = c.get(path).json()
                    assert caps["shell"] is True
                    assert caps["shellAuth"] == "locked"
                exc = _rejected(c, SHELL + "?cmd=id")
                assert (exc.code, exc.reason) == (4401, "shell access is not configured")
                # Even a bearer header does not open a locked shell.
                assert _rejected(c, SHELL, headers=BEARER).code == 4401
        errors = [r for r in caplog.records if r.levelno == logging.ERROR and "no access mode" in r.getMessage()]
        assert errors, "locked shell must log an ERROR at startup"
        assert result.shell_session_manager.active_sessions == 0

    def test_anonymous_requires_explicit_opt_in_and_warns(self, caplog):
        result = _server(allow_anonymous=True)
        with caplog.at_level(logging.INFO):
            with TestClient(result.app) as c:
                assert c.get("/api/qcli/capabilities").json()["shellAuth"] == "anonymous"
                out, code = _run(c, SHELL + "?cmd=echo%20anon-ok")
        assert "anon-ok" in out and code == 0
        assert any(
            r.levelno == logging.WARNING and "without authentication" in r.getMessage()
            for r in caplog.records
        )

    def test_token_via_header(self):
        result = _server(token=SECRET)
        with TestClient(result.app) as c:
            assert c.get("/api/qcli/capabilities").json()["shellAuth"] == "token"
            out, code = _run(c, SHELL + "?cmd=echo%20token-ok", headers=BEARER)
            assert "token-ok" in out and code == 0

    def test_token_via_query_parameter(self):
        result = _server(token=SECRET)
        with TestClient(result.app) as c:
            url = SHELL + f"?cmd=echo%20query-ok&_header_Authorization=Bearer%20{SECRET}"
            out, code = _run(c, url)
            assert "query-ok" in out and code == 0

    def test_token_missing_or_wrong(self):
        result = _server(token=SECRET)
        with TestClient(result.app) as c:
            exc = _rejected(c, SHELL)
            assert (exc.code, exc.reason) == (4401, "login required")
            exc = _rejected(c, SHELL, headers={"Authorization": "Bearer wrong"})
            assert (exc.code, exc.reason) == (4403, "forbidden")
            assert _rejected(c, SHELL + "?_header_Authorization=Bearer%20nope").code == 4403
            assert _rejected(c, SHELL, headers={"Authorization": SECRET}).code == 4401  # no scheme
            assert result.shell_session_manager.active_sessions == 0

    def test_token_uses_constant_time_compare(self, monkeypatch):
        calls = []
        real = shell_security.hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(shell_security.hmac, "compare_digest", spy)
        result = _server(token=SECRET)
        with TestClient(result.app) as c:
            assert _rejected(c, SHELL, headers={"Authorization": "Bearer wrong"}).code == 4403
        import hashlib
        assert calls == [(hashlib.sha256(b"wrong").digest(), hashlib.sha256(SECRET.encode()).digest())]

    def test_token_is_never_logged(self, caplog):
        result = _server(token=SECRET)
        with caplog.at_level(logging.DEBUG):
            with TestClient(result.app) as c:
                _run(c, SHELL + "?cmd=true", headers=BEARER)
                _rejected(c, SHELL, headers={"Authorization": "Bearer wrong-guess"})
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET not in text and "wrong-guess" not in text
        assert SECRET not in repr(result.options.shell)

    def test_authorize_sync_and_async(self):
        seen = []

        def allow_header(ws):
            seen.append(ws.client.host)
            return ws.headers.get("x-shell-key") == "ok"

        result = _server(authorize=allow_header)
        with TestClient(result.app) as c:
            assert c.get("/api/qcli/capabilities").json()["shellAuth"] == "custom"
            out, code = _run(c, SHELL + "?cmd=echo%20custom-ok", headers={"x-shell-key": "ok"})
            assert "custom-ok" in out and code == 0
            exc = _rejected(c, SHELL, headers={"x-shell-key": "no"})
            assert (exc.code, exc.reason) == (4403, "forbidden")
        assert seen

        async def allow_async(ws):
            return ws.query_params.get("k") == "1"

        result = _server(authorize=allow_async)
        with TestClient(result.app) as c:
            out, _ = _run(c, SHELL + "?k=1&cmd=echo%20async-ok")
            assert "async-ok" in out
            assert _rejected(c, SHELL + "?k=0").code == 4403

    def test_authorize_exception_denies(self, caplog):
        def boom(ws):
            raise RuntimeError("database down")

        async def boom_async(ws):
            raise ValueError("nope")

        for fn in (boom, boom_async):
            result = _server(authorize=fn)
            with TestClient(result.app) as c:
                assert _rejected(c, SHELL).code == 4403

    def test_token_and_authorize_both_required(self):
        result = _server(token=SECRET, authorize=lambda ws: ws.headers.get("x-ok") == "1")
        with TestClient(result.app) as c:
            assert c.get("/api/qcli/capabilities").json()["shellAuth"] == "token"
            assert _rejected(c, SHELL, headers={**BEARER, "x-ok": "0"}).code == 4403
            assert _rejected(c, SHELL, headers={"x-ok": "1"}).code == 4401
            out, _ = _run(c, SHELL + "?cmd=echo%20both-ok", headers={**BEARER, "x-ok": "1"})
            assert "both-ok" in out

    def test_anonymous_flag_ignored_when_token_configured(self):
        result = _server(token=SECRET, allow_anonymous=True)
        with TestClient(result.app) as c:
            assert c.get("/api/qcli/capabilities").json()["shellAuth"] == "token"
            assert _rejected(c, SHELL).code == 4401


# ---------------------------------------------------------------------------
# C3 — limits, timeouts, environment
# ---------------------------------------------------------------------------

class TestLimits:
    def test_per_address_limit(self):
        result = _server(allow_anonymous=True, max_sessions_per_user=2)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL) as a, c.websocket_connect(SHELL) as b:
                assert json.loads(a.receive_text())["type"] == "ready"
                assert json.loads(b.receive_text())["type"] == "ready"
                exc = _rejected(c, SHELL)
                assert (exc.code, exc.reason) == (4429, "too many shell sessions")
            # Slots are released when shells close.
            out, _ = _run(c, SHELL + "?cmd=echo%20again")
            assert "again" in out

    def test_per_address_uses_forwarded_for_only_with_trust_proxy(self):
        result = _server(allow_anonymous=True, max_sessions_per_user=1, trust_proxy=True)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL, headers={"x-forwarded-for": "10.0.0.1"}) as a:
                assert json.loads(a.receive_text())["type"] == "ready"
                assert _rejected(c, SHELL, headers={"x-forwarded-for": "10.0.0.1"}).code == 4429
                with c.websocket_connect(SHELL, headers={"x-forwarded-for": "10.0.0.2"}) as b:
                    assert json.loads(b.receive_text())["type"] == "ready"

    def test_spoofed_leftmost_forwarded_for_does_not_bypass_per_address_limit(self):
        result = _server(allow_anonymous=True, max_sessions_per_user=1, trust_proxy=True)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL, headers={"x-forwarded-for": "1.1.1.1, 10.0.0.1"}) as a:
                assert json.loads(a.receive_text())["type"] == "ready"
                exc = _rejected(c, SHELL, headers={"x-forwarded-for": "1.1.1.2, 10.0.0.1"})
                assert exc.code == 4429

    @pytest.mark.parametrize("trust,values,expected", [
        (False, ["1.1.1.1"], "192.0.2.10"),
        (0, ["1.1.1.1"], "192.0.2.10"),
        (None, ["1.1.1.1"], "192.0.2.10"),
        (True, ["1.1.1.1, 10.0.0.1"], "10.0.0.1"),
        (1, ["1.1.1.1, 10.0.0.1"], "10.0.0.1"),
        (2, ["1.1.1.1,2.2.2.2 , 10.0.0.1"], "2.2.2.2"),
        (3, ["1.1.1.1, 10.0.0.1"], "1.1.1.1"),
        (True, [], "192.0.2.10"),
        (True, [""], "192.0.2.10"),
        (True, ["1.1.1.1", "10.0.0.1"], "10.0.0.1"),
        (-1, ["1.1.1.1, 10.0.0.1"], "10.0.0.1"),
        (True, ["1.1.1.1, "], "192.0.2.10"),
    ])
    def test_remote_address_hop_semantics(self, trust, values, expected):
        from types import SimpleNamespace

        from starlette.datastructures import Headers

        ws = SimpleNamespace(
            headers=Headers(raw=[(b"x-forwarded-for", v.encode()) for v in values]),
            client=SimpleNamespace(host="192.0.2.10"),
        )
        assert shell_security.remote_address(ws, trust) == expected

    def test_global_limit(self):
        result = _server(allow_anonymous=True, max_sessions=1, trust_proxy=True)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL, headers={"x-forwarded-for": "10.0.0.1"}) as a:
                assert json.loads(a.receive_text())["type"] == "ready"
                assert _rejected(c, SHELL, headers={"x-forwarded-for": "10.0.0.2"}).code == 4429

    def test_idle_timeout_closes_and_kills(self):
        # Generous enough for slow CI runners to start the shell and report its pid first
        result = _server(allow_anonymous=True, idle_timeout=2.0)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL) as ws:
                pid = _shell_pid(ws)
                start = time.monotonic()
                messages, exc = _until_closed(ws)
                assert (exc.code, exc.reason) == (4408, "shell timed out")
                assert time.monotonic() - start < 8
                assert not any(m["type"] == "exit" for m in messages)
            assert _gone(pid), "shell process must be killed"
            assert result.shell_session_manager.active_sessions == 0

    def test_stdin_resets_idle_timer(self):
        result = _server(allow_anonymous=True, idle_timeout=0.8)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL) as ws:
                _shell_pid(ws)
                for _ in range(4):
                    time.sleep(0.4)
                    ws.send_text(json.dumps({"type": "stdin", "data": "\n"}))
                # 1.6 s elapsed, still open: a command still runs.
                ws.send_text(json.dumps({"type": "stdin", "data": "echo still-open\n"}))
                out = ""
                while "still-open" not in out.replace("echo still-open", ""):
                    msg = json.loads(ws.receive_text())
                    out += msg.get("data", "")
                _, exc = _until_closed(ws)
                assert exc.code == 4408

    def test_max_duration(self):
        result = _server(allow_anonymous=True, idle_timeout=None, max_duration=0.6)
        with TestClient(result.app) as c:
            with c.websocket_connect(SHELL + "?cmd=sleep%2030") as ws:
                assert json.loads(ws.receive_text())["type"] == "ready"
                _, exc = _until_closed(ws)
                assert exc.code == 4408

    def test_minimal_environment(self, monkeypatch):
        monkeypatch.setenv("SECRET_X", "do-not-leak")
        monkeypatch.setenv("DATABASE_PASSWORD", "hunter2")
        monkeypatch.setenv("ALLOWED_X", "visible")
        result = _server(allow_anonymous=True, env_allow=["ALLOWED_X"], env_set={"FOO": "bar"})
        with TestClient(result.app) as c:
            out, code = _run(c, SHELL + "?cols=100&rows=30&cmd=env")
        assert code == 0
        names = {line.split("=", 1)[0] for line in out.replace("\r", "").splitlines() if "=" in line}
        assert "SECRET_X" not in names and "do-not-leak" not in out
        assert "DATABASE_PASSWORD" not in names
        assert "ALLOWED_X=visible" in out and "FOO=bar" in out
        assert "TERM=xterm-256color" in out
        assert "COLUMNS=100" in out and "LINES=30" in out
        assert "SHELL=/bin/sh" in out
        allowed = {"PATH", "HOME", "USER", "LANG", "TERM", "COLUMNS", "LINES", "SHELL", "ALLOWED_X", "FOO",
                   # set by the shell itself
                   "PWD", "OLDPWD", "SHLVL", "_", "PS1", "PS2", "PS4", "OPTIND", "IFS", "PPID",
                   # per-shell process marker
                   "QCLI_SHELL_ID"}
        assert names <= allowed, names - allowed
        assert re.search(r"QCLI_SHELL_ID=[0-9a-f]{32}\b", out)

    def test_shell_id_marker_cannot_be_overridden(self, monkeypatch):
        monkeypatch.setenv("QCLI_SHELL_ID", "server-value")
        manager = _server(
            allow_anonymous=True, env_allow=["QCLI_SHELL_ID"], env_set={"QCLI_SHELL_ID": "set-value"},
        ).shell_session_manager
        env = manager.build_env("/bin/sh", 80, 24, shell_id="ab" * 16)
        assert env["QCLI_SHELL_ID"] == "ab" * 16

    def test_build_env_ignores_server_secrets(self, monkeypatch):
        monkeypatch.setenv("SECRET_X", "do-not-leak")
        env = _server(allow_anonymous=True).shell_session_manager.build_env("/bin/sh", 80, 24)
        assert "SECRET_X" not in env
        assert set(env) <= {"PATH", "HOME", "USER", "LANG", "TERM", "COLUMNS", "LINES", "SHELL"}

    def test_cwd_option(self, tmp_path):
        result = _server(allow_anonymous=True, cwd=str(tmp_path))
        with TestClient(result.app) as c:
            out, code = _run(c, SHELL + "?cmd=pwd%20-P")
        assert code == 0
        assert os.path.realpath(tmp_path) in out

    def test_logs_open_close_without_command_text(self, caplog):
        result = _server(token=SECRET)
        with caplog.at_level(logging.DEBUG):
            with TestClient(result.app) as c:
                _run(c, SHELL + "?cmd=echo%20SENTINEL_COMMAND_TEXT", headers=BEARER)
        messages = [r.getMessage() for r in caplog.records]
        opened = [m for m in messages if m.startswith("Shell opened")]
        closed = [m for m in messages if m.startswith("Shell closed")]
        assert opened and "auth=token" in opened[0] and "remote=testclient" in opened[0]
        assert closed and "exitCode=0" in closed[0]
        assert not any("SENTINEL_COMMAND_TEXT" in m for m in messages)


# ---------------------------------------------------------------------------
# Closing a shell kills its whole process tree
# ---------------------------------------------------------------------------

def _background_pids(ws, commands: dict[str, str]) -> dict[str, int]:
    """Run ``commands`` (label -> shell line printing ``<label>=<pid>``) in
    an interactive shell; returns label -> pid."""
    import re

    assert json.loads(ws.receive_text())["type"] == "ready"
    for line in commands.values():
        ws.send_text(json.dumps({"type": "stdin", "data": line + "\n"}))
    pattern = re.compile(r"\b(" + "|".join(commands) + r")=(\d+)")
    pids: dict[str, int] = {}
    out = ""
    deadline = time.monotonic() + 10
    while len(pids) < len(commands) and time.monotonic() < deadline:
        msg = json.loads(ws.receive_text())
        out += msg.get("data", "")
        pids = {label: int(pid) for label, pid in pattern.findall(out)}
    assert len(pids) == len(commands), f"pids not reported: {out!r}"
    return pids


def _sleep_argv(seconds: int) -> list[str]:
    """A sleeping process whose environment the server can read: macOS
    hides the environment of platform binaries such as /bin/sleep from
    ``ps -E``, so a Python sleep is used there."""
    import sys

    if sys.platform == "darwin":
        return [sys.executable, "-c", f"import time; time.sleep({seconds})"]
    return ["sleep", str(seconds)]


class TestProcessTree:
    def test_background_jobs_are_killed_when_the_shell_closes(self):
        """``&`` jobs get their own process groups (job control) and
        ``nohup`` / ``setsid`` children escape the shell's group: closing the
        shell must still kill them."""
        import shutil
        import sys

        commands = {
            "BG": "sleep 7301 & echo BG=$!",
            "NH": "nohup sleep 7302 >/dev/null 2>&1 & echo NH=$!",
            # A child in its own session (setsid) whose parent stays alive.
            "SS": (
                f"{sys.executable} -c \"import os; p = os.fork(); "
                f"(os.setsid(), os.execvp('sleep', ['sleep', '7303'])) if p == 0 "
                f"else (print('SS=%d' % p, flush=True), os.wait())\" &"
            ),
        }
        if shutil.which("setsid"):
            commands["SW"] = "setsid -w sleep 7304 & echo SW=$!"
        # ``setsid sleep &`` the util-linux way: the job is a process-group
        # leader, so setsid forks, the parent exits and the child (re-parented
        # to init, in a new session) runs the command.
        commands["SL"] = (
            f"{sys.executable} -c \"import os; "
            f"os.getpgrp() == os.getpid() or os.setpgid(0, 0); "
            f"os.fork() and os._exit(0); os.setsid(); "
            f"print('SL=%d' % os.getpid(), flush=True); "
            f"os.execvp({_sleep_argv(7307)[0]!r}, {_sleep_argv(7307)!r})\" &"
        )
        result = _server(allow_anonymous=True)
        pids: dict[str, int] = {}
        try:
            with TestClient(result.app) as c:
                with c.websocket_connect(SHELL) as ws:
                    pids = _background_pids(ws, commands)
                    time.sleep(0.3)  # let the setsid children exec
                    for pid in pids.values():
                        os.kill(pid, 0)  # alive while the shell is open
                assert all(_gone(pid) for pid in pids.values()), pids
        finally:
            for pid in pids.values():
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

    def test_marker_finds_reparented_process(self):
        import subprocess
        import sys

        from qodalis_cli.services.cli_shell_session_manager import _collect_targets, _marked_pids

        shell_id = os.urandom(16).hex()
        env = dict(os.environ, QCLI_SHELL_ID=shell_id)
        # Double fork: the sleep is re-parented and in its own session.
        launcher = subprocess.run(
            [sys.executable, "-c",
             "import os, sys\n"
             "if os.fork() == 0:\n"
             "    os.setsid()\n"
             f"    os.execvp({_sleep_argv(7308)[0]!r}, {_sleep_argv(7308)!r})\n"
             "print(1)"],
            env=env, stdout=subprocess.DEVNULL, check=True,
        )
        assert launcher.returncode == 0
        found: set[int] = set()
        try:
            deadline = time.monotonic() + 5
            while not found and time.monotonic() < deadline:
                found = _marked_pids(shell_id)
                time.sleep(0.05)
            assert len(found) == 1, found
            assert found <= _collect_targets(999999, shell_id, include_tree=True)
            assert not _marked_pids(shell_id[:-1])
        finally:
            for pid in found:
                os.kill(pid, 9)

    def test_process_tree_lists_all_descendants(self):
        import subprocess

        from qodalis_cli.services.cli_shell_session_manager import _process_tree

        parent = subprocess.Popen(["/bin/sh", "-c", "sleep 7305 & sleep 7306 & wait"])
        try:
            time.sleep(0.3)
            tree = _process_tree(parent.pid)
            assert len(tree) == 2
            assert parent.pid not in tree
        finally:
            for pid in _process_tree(parent.pid):
                os.kill(pid, 9)
            parent.kill()
            parent.wait()


# ---------------------------------------------------------------------------
# PTY I/O must never block the event loop
# ---------------------------------------------------------------------------

class _StdinSocket:
    """Fake WebSocket delivering the given text frames, then idling."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)

    async def receive_text(self) -> str:
        import asyncio

        if self._frames:
            return self._frames.pop(0)
        await asyncio.Event().wait()
        return ""  # pragma: no cover


class TestPtyIO:
    async def test_stdin_larger_than_pty_buffer_is_written_without_dropping_the_shell(self):
        """A full PTY input buffer (process not reading stdin) must not end
        the shell nor lose data: writes wait for the fd to drain."""
        import asyncio
        import threading

        from qodalis_cli.services.cli_shell_session_manager import _ShellSession

        manager = _server(allow_anonymous=True).shell_session_manager
        r, w = os.pipe()
        os.set_blocking(w, False)  # a PTY master is non-blocking
        payload = "x" * (512 * 1024)
        received = bytearray()

        def drain() -> None:
            time.sleep(0.3)  # the "process" starts reading stdin later
            while len(received) < len(payload):
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                received.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        session = _ShellSession(
            id="t", mode="anonymous", kind="interactive", key="k", address="a",
            websocket=None, loop=asyncio.get_running_loop(),
        )
        socket = _StdinSocket([json.dumps({"type": "stdin", "data": payload})])
        task = asyncio.ensure_future(manager._read_websocket_input(socket, w, session))
        try:
            deadline = time.monotonic() + 5
            while len(received) < len(payload) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not task.done(), "input handler gave up on a full PTY buffer"
            assert len(received) == len(payload)
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            reader.join(2)
            os.close(r)
            os.close(w)

    def test_pty_master_is_non_blocking(self, monkeypatch):
        """A blocking master fd would freeze the whole server when the PTY
        input buffer fills up (Linux)."""
        from qodalis_cli.services import cli_shell_session_manager as mod

        seen: list[bool] = []
        original = mod.CliShellSessionManager._read_websocket_input

        async def spy(self, websocket, master_fd, session):
            seen.append(os.get_blocking(master_fd))
            return await original(self, websocket, master_fd, session)

        monkeypatch.setattr(mod.CliShellSessionManager, "_read_websocket_input", spy)
        result = _server(allow_anonymous=True)
        with TestClient(result.app) as c:
            _run(c, SHELL + "?cmd=echo%20nb")
        assert seen == [False]

    async def test_output_is_not_read_faster_than_it_is_sent(self):
        """Backpressure: while a send to a slow client is pending, no more
        PTY output is read, so the producer blocks instead of the server
        buffering its output in memory."""
        import asyncio
        import threading

        manager = _server(allow_anonymous=True).shell_session_manager
        r, w = os.pipe()
        os.set_blocking(r, False)  # a PTY master is non-blocking
        written = 0
        chunk = b"y\n" * 8192

        def produce() -> None:  # like `yes`
            nonlocal written
            try:
                while written < 32 * 1024 * 1024:
                    written += os.write(w, chunk)
            except OSError:
                pass

        class _SlowSocket:
            sent = 0

            async def send_text(self, text: str) -> None:
                self.sent += 1
                await asyncio.Event().wait()  # the client never drains

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        task = asyncio.ensure_future(manager._read_pty_output(r, _SlowSocket()))
        try:
            await asyncio.sleep(1.0)
            assert written < 2 * 1024 * 1024, f"server buffered {written} bytes"
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            os.close(r)
            producer.join(2)
            os.close(w)


class _FailingReadySocket:
    """Fake WebSocket whose client goes away before ``ready`` is sent."""

    async def send_text(self, text: str) -> None:
        import asyncio

        await asyncio.sleep(0.5)  # let the shell install its traps
        raise RuntimeError("client gone")


class TestPtyReaping:
    async def test_child_ignoring_hup_and_term_is_reaped_when_setup_fails(self, monkeypatch):
        from qodalis_cli.services import cli_shell_session_manager as mod

        pids: list[int] = []
        original_fork = mod.pty.fork

        def fork():
            pid, fd = original_fork()
            if pid:
                pids.append(pid)
            return pid, fd

        monkeypatch.setattr(mod.pty, "fork", fork)
        manager = _server(allow_anonymous=True).shell_session_manager
        await manager.handle_session(
            _FailingReadySocket(), 80, 24, "trap '' HUP TERM; sleep 30",
        )
        assert pids
        time.sleep(0.5)
        try:
            pid, _ = os.waitpid(pids[0], os.WNOHANG)
        except ChildProcessError:
            return  # already reaped
        if pid == 0:
            os.kill(pids[0], 9)
            os.waitpid(pids[0], 0)
            pytest.fail("shell process still running")
        pytest.fail("shell process left as a zombie")


class TestStdinEncoding:
    async def test_lone_surrogate_in_stdin_does_not_end_the_shell(self):
        import asyncio

        from qodalis_cli.services.cli_shell_session_manager import _ShellSession

        manager = _server(allow_anonymous=True).shell_session_manager
        r, w = os.pipe()
        os.set_blocking(w, False)
        session = _ShellSession(
            id="t", mode="anonymous", kind="interactive", key="k", address="a",
            websocket=None, loop=asyncio.get_running_loop(),
        )
        socket = _StdinSocket([
            '{"type": "stdin", "data": "a\\ud800"}',
            json.dumps({"type": "stdin", "data": "next"}),
        ])
        task = asyncio.ensure_future(manager._read_websocket_input(socket, w, session))
        try:
            await asyncio.sleep(0.2)
            assert not task.done(), task.exception() if task.done() else None
            assert os.read(r, 100).endswith(b"next")
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            os.close(r)
            os.close(w)


# ---------------------------------------------------------------------------
# Process lookup parsing (/proc, ps) and marker matching
# ---------------------------------------------------------------------------

class TestProcessLookup:
    def test_parse_proc_stat_reads_fields_after_last_paren(self):
        from qodalis_cli.services.cli_shell_session_manager import _ProcInfo, _parse_proc_stat

        text = "4242 (we) ird) (name) S 17 4200 4100 34816 4242 4194560 0 0\n"
        assert _parse_proc_stat(4242, text) == _ProcInfo(4242, 17, 4200, 4100)
        assert _parse_proc_stat(1, "garbage") is None
        assert _parse_proc_stat(1, "1 (x) S 0") is None
        assert _parse_proc_stat(1, "1 (x) S a b c") is None

    def test_environ_marker_matching(self):
        from qodalis_cli.services.cli_shell_session_manager import _environ_has_marker

        sid = "0123456789abcdef0123456789abcdef"
        assert _environ_has_marker(b"PATH=/bin\0QCLI_SHELL_ID=" + sid.encode() + b"\0HOME=/", sid)
        assert _environ_has_marker(b"QCLI_SHELL_ID=" + sid.encode(), sid)
        assert not _environ_has_marker(b"QCLI_SHELL_ID=" + sid.encode() + b"0\0", sid)
        assert not _environ_has_marker(b"XQCLI_SHELL_ID=" + sid.encode() + b"\0", sid)
        assert not _environ_has_marker(b"FOO=QCLI_SHELL_ID=" + sid.encode(), sid)
        assert not _environ_has_marker(b"", sid)

    def test_ps_marker_matching_requires_whole_token(self):
        from qodalis_cli.services.cli_shell_session_manager import _ps_marked_pids

        sid = "0123456789abcdef0123456789abcdef"
        out = (
            f"  101 sleep 10 PATH=/bin QCLI_SHELL_ID={sid} HOME=/\n"
            f"  102 sleep 10 QCLI_SHELL_ID={sid}0\n"
            f"  103 sleep 10 XQCLI_SHELL_ID={sid}\n"
            f"  104 sleep 10 QCLI_SHELL_ID={sid}\n"
            f"QCLI_SHELL_ID={sid}\n"
            f"  105 -bash\n"
        )
        assert _ps_marked_pids(out, sid) == {101, 104}

    def test_parse_ps_table(self):
        from qodalis_cli.services.cli_shell_session_manager import _ProcInfo, _parse_ps_table

        out = "    1     0     1\n  200     1   200\nbad line\n  300   200\n"
        assert _parse_ps_table(out) == [_ProcInfo(1, 0, 1, None), _ProcInfo(200, 1, 200, None)]

    def test_proc_fixture_lookup(self, tmp_path, monkeypatch):
        from qodalis_cli.services import cli_shell_session_manager as m

        sid = "f" * 32
        entries = {
            # pid: (stat, environ or None when unreadable)
            500: ("500 (bash) S 1 500 500 0", b"QCLI_SHELL_ID=" + sid.encode() + b"\0"),
            501: ("501 (sleep) S 500 501 500 0", b"PATH=/bin\0"),          # job: own group, same session
            502: ("502 (my (odd) name) S 1 502 502 0", b"QCLI_SHELL_ID=" + sid.encode()),  # setsid'd orphan
            503: ("503 (other) S 1 503 503 0", b"QCLI_SHELL_ID=" + b"e" * 32),
            504: ("504 (secret) S 1 504 504 0", None),
        }
        (tmp_path / "self").mkdir()
        (tmp_path / "self" / "stat").write_text("1 (x) S 0 1 1 0")
        for pid, (stat, environ) in entries.items():
            d = tmp_path / str(pid)
            d.mkdir()
            (d / "stat").write_text(stat)
            if environ is not None:
                (d / "environ").write_bytes(environ)
        monkeypatch.setattr(m, "_PROC", str(tmp_path))
        procs = {p.pid: p for p in m._list_processes()}
        assert procs[501] == m._ProcInfo(501, 500, 501, 500)
        assert procs[502].ppid == 1
        assert m._marked_pids(sid) == {500, 502}
        assert m._descendants(list(procs.values()), 500) == [501]
        assert m._collect_targets(500, sid, include_tree=True) == {500, 501, 502}
        assert m._collect_targets(500, sid, include_tree=False) == {500, 501, 502}  # group/session
        assert m._collect_targets(500, None, include_tree=False) == {500, 501}

    def test_collect_targets_never_includes_the_server(self, monkeypatch):
        from qodalis_cli.services import cli_shell_session_manager as m

        own, group = os.getpid(), os.getpgrp()
        procs = [
            m._ProcInfo(own, 1, group, None),
            m._ProcInfo(700, own, 700, 700),
            m._ProcInfo(701, 1, group, None),  # another member of the server's group
        ]
        monkeypatch.setattr(m, "_list_processes", lambda: procs)
        monkeypatch.setattr(m, "_marked_pids", lambda _sid: {own, 701, 1, 702})
        assert m._collect_targets(700, "x", include_tree=True) == {700, 702}
