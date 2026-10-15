"""Interactive PTY shell sessions over WebSocket, with the Appendix C
security model (access modes, session binding, limits, minimal environment,
audit)."""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import json
import logging
import os
import platform
import pty
import pwd
import signal
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from ..shell_security import (
    BASE_ENV_NAMES,
    CLOSE_DISABLED,
    CLOSE_FORBIDDEN,
    CLOSE_LIMIT,
    CLOSE_TIMEOUT,
    CLOSE_UNAUTHORIZED,
    MAX_SESSION_CHECK_INTERVAL,
    MODE_ANONYMOUS,
    MODE_LOCKED,
    MODE_USERS,
    REASON_DISABLED,
    REASON_FORBIDDEN,
    REASON_LIMIT,
    REASON_LOCKED,
    REASON_LOGIN_REQUIRED,
    REASON_SESSION_ENDED,
    REASON_TIMEOUT,
    DEFAULT_SESSION_CHECK_INTERVAL,
    END_CLOSED,
    END_ERROR,
    END_EXITED,
    END_SESSION_ENDED,
    END_SHUTDOWN,
    END_TIMED_OUT,
    TOUCH_INTERVAL,
    CliShellOptions,
    ShellAccessProvider,
    ShellPrincipal,
    ShellRejected,
    check_authorize,
    check_token,
    remote_address,
    resolve_mode,
)

logger = logging.getLogger(__name__)

_LOCKED_MESSAGE = (
    "Remote shell is enabled but no access mode is configured, so every shell "
    "connection is refused (close 4401). Mount the users plugin (requires the "
    "shell:execute permission), or set CliServerOptions(shell=CliShellOptions("
    "token='<secret>')) or CliShellOptions(authorize=callable), or - unsafe - "
    "CliShellOptions(allow_anonymous=True)."
)
_ANONYMOUS_MESSAGE = (
    "WARNING: shell is enabled without authentication (allow_anonymous=True): "
    "anyone who can reach this server gets an OS shell."
)


@dataclass
class _ShellSession:
    id: str
    mode: str
    kind: str
    key: str
    address: str
    websocket: Any
    loop: asyncio.AbstractEventLoop
    principal: ShellPrincipal | None = None
    started: float = field(default_factory=time.monotonic)
    last_input: float = field(default_factory=time.monotonic)
    close_event: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int | None = None
    close_reason: str | None = None
    end_reason: str = END_EXITED
    #: monotonic time of the last session activity reported to the provider
    last_touch: float = field(default_factory=time.monotonic)

    def request_close(self, code: int, reason: str, end_reason: str) -> None:
        if self.close_event.is_set():
            return
        self.close_code, self.close_reason, self.end_reason = code, reason, end_reason
        self.close_event.set()


class CliShellSessionManager:
    """Manages interactive PTY shell sessions over WebSocket connections.

    :meth:`handle_connection` is the secured entry point used by the server
    routes (access mode, limits, audit).  :meth:`handle_session` only runs a
    PTY for an already-authorized socket.
    """

    def __init__(self, options: CliShellOptions | None = None, *, enabled: bool = True) -> None:
        self.options = options or CliShellOptions()
        self.enabled = enabled
        self._provider: ShellAccessProvider | None = None
        self._sessions: dict[str, _ShellSession] = {}
        self._locked_logged = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_access_provider(self, provider: ShellAccessProvider | None) -> None:
        """Register the auth plugin that owns shell access (users mode)."""
        self._provider = provider
        add_listener = getattr(provider, "add_change_listener", None)
        if callable(add_listener):
            add_listener(self.request_recheck)

    @property
    def access_provider(self) -> ShellAccessProvider | None:
        return self._provider

    @property
    def access_mode(self) -> str | None:
        """``users``/``token``/``custom``/``anonymous``/``locked``, or
        ``None`` when the shell is disabled."""
        if not self.enabled:
            return None
        return resolve_mode(self.options, self._provider is not None)

    @property
    def session_check_interval(self) -> float:
        """Seconds between session re-checks of users-mode shells (default
        30, at most 60)."""
        value = self.options.session_check_interval
        if not value or value <= 0:
            return DEFAULT_SESSION_CHECK_INTERVAL
        return min(value, MAX_SESSION_CHECK_INTERVAL)

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    def log_startup(self) -> None:
        """Log the shell's access mode once the plugins are mounted."""
        mode = self.access_mode
        if mode is None:
            return
        if mode == MODE_LOCKED:
            self._locked_logged = True
            logger.error(_LOCKED_MESSAGE)
        elif mode == MODE_ANONYMOUS:
            logger.warning(_ANONYMOUS_MESSAGE)
        else:
            logger.info("Remote shell enabled (auth=%s)", mode)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle_connection(self, websocket: WebSocket) -> None:
        """Authorize and run one shell connection (accept, then close with a
        4xxx code on refusal)."""
        if websocket.client_state.name == "CONNECTING":
            await websocket.accept()
        if not self.enabled:
            await _close(websocket, CLOSE_DISABLED, REASON_DISABLED)
            return

        cols = _int_param(websocket, "cols", 80)
        rows = _int_param(websocket, "rows", 24)
        command = websocket.query_params.get("cmd") or None  # never logged
        address = remote_address(websocket, self.options.trust_proxy)

        try:
            mode, principal = await self._authorize(websocket)
            session = self._register(websocket, mode, principal, address, command)
        except ShellRejected as rejected:
            await _close(websocket, rejected.code, rejected.reason)
            return

        await self._opened(session)
        exit_code: int | None = None
        try:
            exit_code = await self.handle_session(websocket, cols, rows, command, _session=session)
        finally:
            self._sessions.pop(session.id, None)
            await self._closed(session, exit_code)
        if session.close_code is not None:
            await _close(websocket, session.close_code, session.close_reason or "")

    async def _authorize(self, websocket: WebSocket) -> tuple[str, ShellPrincipal | None]:
        opts = self.options
        mode = resolve_mode(opts, self._provider is not None)
        if mode == MODE_LOCKED:
            if not self._locked_logged:
                self._locked_logged = True
                logger.error(_LOCKED_MESSAGE)
            raise ShellRejected(CLOSE_UNAUTHORIZED, REASON_LOCKED)

        principal: ShellPrincipal | None = None
        if mode == MODE_USERS:
            assert self._provider is not None
            try:
                principal = await self._provider.resolve(websocket)
            except Exception:
                logger.exception("Shell access provider failed to resolve the caller")
                principal = None
            if principal is None:
                raise ShellRejected(CLOSE_UNAUTHORIZED, REASON_LOGIN_REQUIRED)
            if not self._provider.can_open(principal):
                raise ShellRejected(CLOSE_FORBIDDEN, REASON_FORBIDDEN)

        # token / authorize: every configured check must pass.
        if mode != MODE_ANONYMOUS:
            if opts.token:
                check_token(websocket, opts.token)
            if opts.authorize is not None:
                await check_authorize(websocket, opts.authorize)
        return mode, principal

    def _register(
        self,
        websocket: WebSocket,
        mode: str,
        principal: ShellPrincipal | None,
        address: str,
        command: str | None,
    ) -> _ShellSession:
        """Count and register a session (synchronous: no interleaving)."""
        opts = self.options
        key = f"user:{principal.user_id}" if principal else f"addr:{address}"
        if opts.max_sessions and len(self._sessions) >= opts.max_sessions:
            raise ShellRejected(CLOSE_LIMIT, REASON_LIMIT)
        if opts.max_sessions_per_user and (
            sum(1 for s in self._sessions.values() if s.key == key) >= opts.max_sessions_per_user
        ):
            raise ShellRejected(CLOSE_LIMIT, REASON_LIMIT)
        session = _ShellSession(
            id=str(uuid.uuid4()),
            mode=mode,
            kind="command" if command else "interactive",
            key=key,
            address=address,
            websocket=websocket,
            loop=asyncio.get_running_loop(),
            principal=principal,
        )
        self._sessions[session.id] = session
        return session

    # ------------------------------------------------------------------
    # Session binding (C2)
    # ------------------------------------------------------------------

    def request_recheck(self) -> None:
        """Re-check every users-mode shell now (called on session events;
        thread-safe)."""
        for session in list(self._sessions.values()):
            if session.principal is None or session.close_event.is_set():
                continue
            try:
                session.loop.call_soon_threadsafe(self._spawn_check, session)
            except RuntimeError:
                pass  # loop closed

    def _spawn_check(self, session: _ShellSession) -> None:
        if not session.close_event.is_set():
            asyncio.ensure_future(self._check(session))

    async def _check(self, session: _ShellSession) -> None:
        ended = await self._end_reason(session)
        if ended is not None:
            session.request_close(CLOSE_UNAUTHORIZED, REASON_SESSION_ENDED, ended)

    async def _end_reason(self, session: _ShellSession) -> str | None:
        """``None`` while the owning session is valid, else the audit reason."""
        if session.principal is None or self._provider is None:
            return None
        try:
            result = await self._provider.is_active(session.principal)
        except Exception:
            logger.exception("Shell session check failed; closing the shell")
            return END_ERROR
        if isinstance(result, str):
            return result or END_SESSION_ENDED
        if not result:
            return END_SESSION_ENDED
        # The check itself never extends the session; stdin since the last
        # touch counts as activity (at most once per TOUCH_INTERVAL) so an
        # active shell user is not logged out for inactivity.
        now = time.monotonic()
        if session.last_input > session.last_touch and now - session.last_touch >= TOUCH_INTERVAL:
            session.last_touch = now
            touch = getattr(self._provider, "touch", None)
            if callable(touch):
                try:
                    await touch(session.principal)
                except Exception:
                    logger.debug("Shell session touch failed", exc_info=True)
        return None

    async def _watch(self, session: _ShellSession) -> None:
        """Enforce idle timeout, max duration and the periodic session check;
        returns once the session must be closed."""
        opts = self.options
        idle = opts.idle_timeout or None
        max_duration = opts.max_duration or None
        interval = self.session_check_interval if session.principal is not None else None
        next_check = time.monotonic() + interval if interval else None

        while not session.close_event.is_set():
            now = time.monotonic()
            if idle and now - session.last_input >= idle:
                session.request_close(CLOSE_TIMEOUT, REASON_TIMEOUT, END_TIMED_OUT)
                break
            if max_duration and now - session.started >= max_duration:
                session.request_close(CLOSE_TIMEOUT, REASON_TIMEOUT, END_TIMED_OUT)
                break
            if next_check is not None and now >= next_check:
                await self._check(session)
                next_check = time.monotonic() + interval
                continue
            deadlines = [
                d for d in (
                    session.last_input + idle if idle else None,
                    session.started + max_duration if max_duration else None,
                    next_check,
                ) if d is not None
            ]
            timeout = max(0.01, min(deadlines) - now) if deadlines else None
            try:
                await asyncio.wait_for(session.close_event.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def close_all(self, reason: str = "server shutdown") -> None:
        """Close every open shell (e.g. on server shutdown)."""
        for session in list(self._sessions.values()):
            session.request_close(1001, reason, END_SHUTDOWN)

    # ------------------------------------------------------------------
    # Audit / logging (C4) — never the command text or stdin
    # ------------------------------------------------------------------

    async def _opened(self, session: _ShellSession) -> None:
        if session.principal is not None and self._provider is not None:
            await self._audit("shell_opened", session, {
                "mode": session.kind,
                "sessionId": session.principal.session_id,
            })
        else:
            logger.info(
                "Shell opened (auth=%s, kind=%s, remote=%s)",
                session.mode, session.kind, session.address,
            )

    async def _closed(self, session: _ShellSession, exit_code: int | None) -> None:
        if session.end_reason != END_EXITED:
            exit_code = None  # exitCode only when the process exited by itself
        duration_ms = int((time.monotonic() - session.started) * 1000)
        if session.principal is not None and self._provider is not None:
            metadata: dict[str, Any] = {
                "mode": session.kind,
                "sessionId": session.principal.session_id,
                "durationMs": duration_ms,
                "reason": session.end_reason,
            }
            if exit_code is not None:
                metadata["exitCode"] = exit_code
            await self._audit("shell_closed", session, metadata)
        else:
            logger.info(
                "Shell closed (auth=%s, kind=%s, remote=%s, durationMs=%d, exitCode=%s, reason=%s)",
                session.mode, session.kind, session.address, duration_ms,
                exit_code if exit_code is not None else "-", session.end_reason,
            )

    async def _audit(self, event: str, session: _ShellSession, metadata: dict[str, Any]) -> None:
        assert self._provider is not None and session.principal is not None
        try:
            await self._provider.audit(event, session.principal, metadata)
        except Exception:
            logger.exception("Failed to audit %s", event)

    # ------------------------------------------------------------------
    # PTY
    # ------------------------------------------------------------------

    def build_env(self, shell: str, cols: int, rows: int) -> dict[str, str]:
        """The child environment: a minimal base plus ``env_allow`` names
        and ``env_set`` values — never the full server environment."""
        source = os.environ
        env = {name: source[name] for name in BASE_ENV_NAMES if name in source}
        if "PATH" not in env:
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
        if "HOME" not in env or "USER" not in env:
            try:
                entry = pwd.getpwuid(os.getuid())
                env.setdefault("HOME", entry.pw_dir)
                env.setdefault("USER", entry.pw_name)
            except KeyError:
                pass
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        env["SHELL"] = shell
        for name in self.options.env_allow or []:
            if name in source:
                env[name] = source[name]
        for name, value in (self.options.env_set or {}).items():
            env[str(name)] = str(value)
        return env

    async def handle_session(
        self,
        websocket: WebSocket,
        cols: int,
        rows: int,
        command: str | None = None,
        *,
        _session: _ShellSession | None = None,
    ) -> int | None:
        """Run a PTY shell bridged to an already-authorized WebSocket.

        Returns the exit code when the process exited by itself.
        """
        session = _session or _ShellSession(
            id=str(uuid.uuid4()), mode="direct", kind="command" if command else "interactive",
            key="", address="", websocket=websocket, loop=asyncio.get_running_loop(),
        )
        shell, args = self._get_shell_info(command, self.options.shell)
        env = self.build_env(shell, cols, rows)
        cwd = self.options.cwd
        master_fd: int | None = None
        child_pid: int | None = None
        exit_task: asyncio.Future | None = None
        tasks: list[asyncio.Future] = []

        try:
            child_pid, master_fd = pty.fork()

            if child_pid == 0:  # pragma: no cover - child process
                try:
                    if cwd:
                        os.chdir(cwd)
                    os.execvpe(shell, [shell] + args, env)
                except BaseException:
                    pass
                finally:
                    os._exit(127)

            # Non-blocking: a full PTY input buffer (process not reading stdin)
            # must not block the event loop (see _write_pty).
            os.set_blocking(master_fd, False)
            logger.info("Shell session started (shell=%s, cols=%d, rows=%d)", os.path.basename(shell), cols, rows)
            self._set_pty_size(master_fd, cols, rows)

            await websocket.send_text(json.dumps({
                "type": "ready",
                "shell": os.path.basename(shell),
                "os": _detect_os(),
            }))

            read_task = asyncio.ensure_future(self._read_pty_output(master_fd, websocket))
            input_task = asyncio.ensure_future(self._read_websocket_input(websocket, master_fd, session))
            exit_task = asyncio.ensure_future(self._wait_for_exit(child_pid, websocket, session))
            watch_task = asyncio.ensure_future(self._watch(session))
            tasks = [read_task, input_task, exit_task, watch_task]

            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            # Output EOF usually means the process is exiting: give it a moment
            # so the client still receives the exit message (one-shot commands)
            if read_task in done and not exit_task.done() and not session.close_event.is_set():
                await asyncio.wait([exit_task, watch_task], timeout=2, return_when=asyncio.FIRST_COMPLETED)

            if input_task.done() and not exit_task.done() and not session.close_event.is_set():
                session.end_reason = END_CLOSED

        except Exception as exc:
            logger.error("Shell session error: %s", exc)
            session.end_reason = END_ERROR
            try:
                await websocket.send_text(json.dumps({"type": "error", "message": "shell session error"}))
            except Exception as send_err:
                logger.debug("Failed to send WebSocket message: %s", send_err)
        finally:
            session.close_event.set()  # stops the watcher; suppresses "exit"
            if child_pid is not None and child_pid > 0:
                await self._terminate(child_pid, exit_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError as e:
                    logger.debug("Failed to close file descriptor: %s", e)
            logger.info("Shell session ended")

        if exit_task is not None and exit_task.done() and not exit_task.cancelled():
            exc = exit_task.exception()
            return None if exc else exit_task.result()
        return None

    async def _terminate(self, pid: int, exit_task: asyncio.Future | None) -> None:
        """Kill the shell's process group (HUP/TERM, then KILL) and reap it."""
        if exit_task is not None and exit_task.done():
            return
        _signal_group(pid, signal.SIGHUP)
        _signal_group(pid, signal.SIGTERM)
        if exit_task is None:
            await asyncio.sleep(0.2)
            _signal_group(pid, signal.SIGKILL)
            _reap(pid)
            return
        await asyncio.wait([exit_task], timeout=1.0)
        if not exit_task.done():
            _signal_group(pid, signal.SIGKILL)
            await asyncio.wait([exit_task], timeout=2.0)

    async def _read_pty_output(self, master_fd: int, websocket: WebSocket) -> None:
        """Forward PTY output to the WebSocket (event-loop reader, no threads)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def on_readable() -> None:
            try:
                data = os.read(master_fd, 4096)
            except BlockingIOError:
                return  # spurious wake-up
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(master_fd)
                queue.put_nowait(None)
            else:
                queue.put_nowait(data)

        loop.add_reader(master_fd, on_readable)
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        await websocket.send_text(json.dumps({"type": "stdout", "data": tail}))
                    break
                text = decoder.decode(chunk)
                if text:
                    await websocket.send_text(json.dumps({"type": "stdout", "data": text}))
        except Exception as e:
            logger.debug("PTY output error: %s", e)
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

    async def _read_websocket_input(
        self, websocket: WebSocket, master_fd: int, session: _ShellSession,
    ) -> None:
        """Write stdin data to the PTY and apply resizes; returns when the
        client goes away."""
        while True:
            try:
                raw = await websocket.receive_text()
            except Exception as e:
                logger.debug("WebSocket input ended: %s", e)
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            try:
                if msg_type == "stdin":
                    data = msg.get("data", "")
                    if isinstance(data, str) and data:
                        session.last_input = time.monotonic()
                        await _write_pty(master_fd, data.encode("utf-8"))
                elif msg_type == "resize":
                    self._set_pty_size(master_fd, _dim(msg.get("cols"), 80), _dim(msg.get("rows"), 24))
            except OSError as e:
                logger.debug("PTY write error: %s", e)
                return

    async def _wait_for_exit(
        self, child_pid: int, websocket: WebSocket, session: _ShellSession,
    ) -> int | None:
        """Poll for the child's exit (no blocking thread), then send the exit
        code unless the server is closing the shell."""
        while True:
            try:
                pid, status = os.waitpid(child_pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if pid != 0:
                break
            await asyncio.sleep(0.05)
        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        if not session.close_event.is_set():
            try:
                await websocket.send_text(json.dumps({"type": "exit", "code": exit_code}))
            except Exception as e:
                logger.debug("Failed to send exit message: %s", e)
        return exit_code

    @staticmethod
    def _set_pty_size(fd: int, cols: int, rows: int) -> None:
        """Set the PTY window size via ioctl."""
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except (OSError, struct.error) as e:
            logger.debug("Failed to resize PTY: %s", e)

    @staticmethod
    def _detect_shell() -> str:
        """Detect the best available shell on the system."""
        env_shell = os.environ.get("SHELL")
        if env_shell and os.path.isfile(env_shell) and os.access(env_shell, os.X_OK):
            return env_shell

        for candidate in ["/bin/bash", "/usr/bin/bash", "/bin/sh"]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        return "/bin/sh"

    @staticmethod
    def _get_shell_info(command: str | None, shell: str | None = None) -> tuple[str, list[str]]:
        """Return the shell executable and arguments for spawning a session."""
        if platform.system() == "Windows":
            shell = shell or "powershell.exe"
            return (shell, ["-Command", command]) if command else (shell, [])

        shell = shell or CliShellSessionManager._detect_shell()
        return (shell, ["-c", command]) if command else (shell, [])


def _detect_os() -> str:
    system = platform.system()
    return "darwin" if system == "Darwin" else "win32" if system == "Windows" else "linux"


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)  # pty.fork() makes the child a session/group leader
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    except OSError:
        pass


async def _write_pty(fd: int, data: bytes) -> None:
    """Write all of ``data`` to a non-blocking PTY master, waiting (without
    blocking the event loop) while its input buffer is full."""
    loop = asyncio.get_running_loop()
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:
            writable = loop.create_future()
            loop.add_writer(fd, lambda: writable.done() or writable.set_result(None))
            try:
                await writable
            finally:
                loop.remove_writer(fd)


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _int_param(websocket: WebSocket, name: str, default: int) -> int:
    return _dim(websocket.query_params.get(name, default), default)


def _dim(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if 0 < number <= 1000 else default


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as e:
        logger.debug("Failed to close shell socket: %s", e)
