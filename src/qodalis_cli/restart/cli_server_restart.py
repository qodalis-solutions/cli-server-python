"""The server's restart service: the changes waiting for a restart (``mark_required`` / ``clear_required``, empty on
every start) and the restart itself: audit ``server.restart``, event ``server:restarting``, a graceful shutdown and
the mode's action (``exit``, ``self``, ``handler``).  What the process does (exiting, running itself again, the
environment and files the supervisor detection reads, the uvicorn server it stops) is injectable, so tests never
exit or exec."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

from qodalis_cli_server_abstractions.audit import AuditEntry
from qodalis_cli_server_abstractions.events import CliEventAudience
from qodalis_cli_server_abstractions.restart import (
    CLI_SERVER_RESTART_AUDIT_EVENT,
    CLI_SERVER_RESTART_CLOSE_CODE,
    CLI_SERVER_RESTART_MODES,
    CLI_SERVER_RESTART_REQUIRED_EVENT,
    CLI_SERVER_RESTARTING_EVENT,
    CliRestartRequirement,
    CliServerRestartError,
    CliServerRestartRequest,
    ICliServerRestart,
)

logger = logging.getLogger("qodalis_cli.restart")

#: The permission the restart events are published to (the dashboard's audience).
_ADMIN_ACCESS = "admin:access"
#: The exit code of ``exit`` mode unless ``exit_code`` says otherwise (EX_TEMPFAIL: "try again").
DEFAULT_RESTART_EXIT_CODE = 75
#: How long the graceful shutdown waits for in-flight requests, in seconds.
DEFAULT_RESTART_DRAIN_TIMEOUT = 10.0
#: How long one shutdown hook may take, in seconds.
_HOOK_TIMEOUT = 10.0
_MAX_REASON_LENGTH = 500
RESTART_NOT_SUPPORTED = "restart not supported: no supervisor detected; set restart.mode"


@dataclass(frozen=True)
class CliServerRestartContext:
    """What the ``handler`` mode's function receives."""

    #: The ids of the changes that were pending.
    pending: list[str]
    #: Who asked (``anonymous`` on a server without sign-in).
    by: str
    reason: Optional[str] = None


def _exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except OSError:
        return False


def _read_file(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _flush() -> None:
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - closed or detached streams
            pass


def _exit(code: int) -> None:
    """Ends the process with ``code`` once the graceful shutdown is done (logs and output flushed first)."""
    _flush()
    os._exit(code)


def _exec(executable: str, args: list[str], env: Mapping[str, str], cwd: str) -> None:
    """Replaces this process with the same command (same pid, so a terminal or ``nohup`` keeps it)."""
    _flush()
    os.chdir(cwd)
    os.execve(executable, args, dict(env))


def _original_args() -> list[str]:
    """The arguments this interpreter was started with after the executable (``sys.orig_argv``, which keeps
    ``-m module`` and interpreter options; ``sys.argv`` on an interpreter without it)."""
    orig = getattr(sys, "orig_argv", None)
    return list(orig[1:]) if orig else list(sys.argv)


def _uvicorn_server() -> Any:
    """The uvicorn server running this app, found through its signal handler (uvicorn installs
    ``Server.handle_exit`` for SIGINT and SIGTERM); ``None`` under anything else."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            handler = signal.getsignal(sig)
        except (ValueError, OSError):  # pragma: no cover - not the main thread on some platforms
            continue
        server = getattr(handler, "__self__", None)
        if server is not None and hasattr(server, "should_exit") and hasattr(server, "config"):
            return server
    return None


@dataclass
class CliServerRestartRuntime:
    """What the process does, injectable so tests never exit or exec: exiting, running the same command again, the
    environment and files the supervisor detection reads, and the uvicorn server a restart stops."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    exists: Callable[[str], bool] = _exists
    read_file: Callable[[str], Optional[str]] = _read_file
    #: Ends the process with a code (``exit`` mode, or 1 when ``self`` cannot run the command again).
    exit: Callable[[int], None] = _exit
    #: ``exec(executable, args, env, cwd)``: replaces the process with the same command (``self`` mode).
    exec: Callable[[str, list[str], Mapping[str, str], str], None] = _exec
    executable: str = field(default_factory=lambda: sys.executable)
    #: The arguments after the executable (``sys.orig_argv[1:]``: interpreter options and ``-m`` kept).
    argv: list[str] = field(default_factory=_original_args)
    cwd: Callable[[], str] = os.getcwd
    #: The server to stop gracefully (``should_exit``): the uvicorn server running the app; ``None`` when there is
    #: none, and the restart service then shuts down on its own.
    find_server: Callable[[], Any] = _uvicorn_server


@dataclass
class CliServerRestartOptions:
    """The ``restart`` option of ``create_cli_server`` (``CliServerOptions(restart=...)``)."""

    #: ``auto`` (default): ``exit`` under a detected supervisor, else not supported; ``exit``, ``self``, ``handler``.
    mode: str = "auto"
    #: Exit code of ``exit`` mode (default 75).
    exit_code: int = DEFAULT_RESTART_EXIT_CODE
    #: ``handler`` mode: runs after the graceful shutdown (sync or async, takes a :class:`CliServerRestartContext`);
    #: it restarts (or exits) the process.
    handler: Optional[Callable[[CliServerRestartContext], Union[None, Awaitable[None]]]] = None
    #: Allow restarting on a server without sign-in (no auth provider protecting it).  Default false: the API and the
    #: command refuse with 403, since anyone could restart it.
    allow_anonymous: bool = False
    #: How long the graceful shutdown waits for in-flight requests, in seconds (default 10).
    drain_timeout: float = DEFAULT_RESTART_DRAIN_TIMEOUT
    #: Replaces what the process does (tests): a :class:`CliServerRestartRuntime` or a dict of its fields.
    runtime: Union[CliServerRestartRuntime, Mapping[str, Any], None] = None

    @staticmethod
    def of(value: Any) -> "CliServerRestartOptions":
        if value is None:
            return CliServerRestartOptions()
        if isinstance(value, CliServerRestartOptions):
            return value
        if isinstance(value, Mapping):
            return CliServerRestartOptions(**dict(value))
        raise TypeError(f"invalid restart options: {value!r}")


@dataclass
class CliServerRestartServices:
    """The parts of the server the graceful shutdown uses (set by ``create_cli_server``)."""

    event_bus: Optional[Callable[[], Any]] = None
    audit: Optional[Callable[[], Any]] = None
    #: True when an auth provider protects the server (callers are signed in).
    is_protected: Optional[Callable[[], bool]] = None
    #: Closes the WebSockets (events, logs, shell) with a code and reason.
    close_sockets: Optional[Callable[[int, str], Awaitable[None]]] = None


def detect_supervisor(runtime: Any) -> Optional[str]:
    """The supervisor this process runs under, from its environment and files: PM2 (``pm_id``), systemd
    (``INVOCATION_ID``), Kubernetes (``KUBERNETES_SERVICE_HOST``, or ``kubepods`` in ``/proc/1/cgroup``), Docker
    (``/.dockerenv``, or ``docker`` in ``/proc/1/cgroup``); ``None`` for none."""
    env = runtime.env
    if env.get("pm_id") is not None:
        return "pm2"
    if env.get("INVOCATION_ID"):
        return "systemd"
    cgroup = runtime.read_file("/proc/1/cgroup") or ""
    if env.get("KUBERNETES_SERVICE_HOST") or re.search(r"kubepods", cgroup):
        return "kubernetes"
    if runtime.exists("/.dockerenv") or re.search(r"docker", cgroup):
        return "docker"
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class CliServerRestart(ICliServerRestart):
    """The server's restart service (``builder.restart``, ``result.restart``)."""

    def __init__(self) -> None:
        self.started_at = _now_iso()
        self._items: dict[str, CliRestartRequirement] = {}
        self._hooks: list[tuple[str, Callable[[], Any]]] = []
        self._options = CliServerRestartOptions()
        self._services = CliServerRestartServices()
        self._runtime = CliServerRestartRuntime()
        self._server: Any = None
        self._in_flight = 0
        self._restarting = False
        self._completion: Optional[asyncio.Future[None]] = None
        #: Set when uvicorn shuts down for a restart: its lifespan shutdown finishes it (:meth:`server_stopped`).
        self._action: Optional[tuple[str, CliServerRestartContext]] = None
        self._tasks: set[asyncio.Future[Any]] = set()

    def configure(self, options: Any = None, services: Optional[CliServerRestartServices] = None) -> "CliServerRestart":
        """Sets the options and the services (``create_cli_server`` calls it once).  Raises ``ValueError`` for an
        unknown mode or a ``handler`` mode without a handler."""
        opts = CliServerRestartOptions.of(options)
        if opts.mode not in CLI_SERVER_RESTART_MODES:
            raise ValueError(f"invalid restart.mode: {opts.mode}")
        if opts.mode == "handler" and not callable(opts.handler):
            raise ValueError("restart.mode 'handler' needs restart.handler")
        self._options = opts
        if services is not None:
            self._services = services
        runtime = opts.runtime
        if isinstance(runtime, CliServerRestartRuntime):
            self._runtime = runtime
        elif isinstance(runtime, Mapping):
            self._runtime = dataclasses.replace(CliServerRestartRuntime(), **dict(runtime))
        else:
            self._runtime = CliServerRestartRuntime()
        return self

    @property
    def restarting(self) -> bool:
        return self._restarting

    @property
    def completion(self) -> Optional[asyncio.Future[None]]:
        """The graceful shutdown and the mode's action of an accepted restart (tests await it); under uvicorn the
        shutdown finishes in the app's lifespan instead."""
        return self._completion

    def attach_server(self, server: Any) -> None:
        """The uvicorn server (``uvicorn.Server``) a restart stops gracefully; by default the one running the app is
        found through its signal handler.  Set it when the server runs outside the main thread."""
        self._server = server

    def on_shutdown(self, name: str, fn: Callable[[], Any]) -> None:
        """Runs ``fn`` (sync or async) during a restart's graceful shutdown, after the sockets closed, in
        registration order (errors are logged): the job schedulers, plugins' ``dispose``, the host's pools."""
        self._hooks.append((name, fn))

    # ------------------------------------------------------------------ status and pending list

    def _resolve_mode(self) -> tuple[Optional[str], Optional[str]]:
        supervisor = detect_supervisor(self._runtime)
        if self._options.mode == "auto":
            return ("exit" if supervisor else None), supervisor
        return self._options.mode, supervisor

    def status(self) -> dict[str, Any]:
        mode, supervisor = self._resolve_mode()
        out: dict[str, Any] = {"supported": mode is not None, "mode": mode, "supervisor": supervisor}
        if mode is None:
            out["reason"] = RESTART_NOT_SUPPORTED.replace("restart not supported: ", "")
        out["startedAt"] = self.started_at
        out["pending"] = [p.to_dict() for p in self.pending()]
        return out

    def pending(self) -> list[CliRestartRequirement]:
        return list(self._items.values())

    def mark_required(self, id: str, source: str, description: str) -> None:
        if not isinstance(id, str) or not id or not isinstance(source, str) or not isinstance(description, str):
            raise ValueError("mark_required needs an id, a source and a description")
        if id in self._items:
            return
        self._items[id] = CliRestartRequirement(id=id, source=source, description=description, since=_now_iso())
        logger.info("Restart required: %s", description)
        self._publish_pending()

    def clear_required(self, id: str) -> bool:
        if self._items.pop(id, None) is None:
            return False
        self._publish_pending()
        return True

    def _publish_pending(self) -> None:
        bus = self._services.event_bus() if self._services.event_bus is not None else None
        if bus is None:
            return
        data = {"pending": [p.to_dict() for p in self.pending()]}
        try:
            result = bus.publish(CLI_SERVER_RESTART_REQUIRED_EVENT, data, CliEventAudience(permission=_ADMIN_ACCESS))
        except Exception as exc:  # noqa: BLE001 - the list changed; the event failing is logged
            logger.error("Publishing %s failed: %s", CLI_SERVER_RESTART_REQUIRED_EVENT, exc)
            return
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No loop (marked from synchronous host code): delivered right away
                asyncio.run(_maybe_await(result))
                return
            task = asyncio.ensure_future(result)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ restarting

    async def restart(self, request: Optional[CliServerRestartRequest] = None) -> dict[str, Any]:
        request = request or CliServerRestartRequest()
        reason = request.reason
        if reason is not None and not isinstance(reason, str):
            raise CliServerRestartError(400, "reason must be a string")
        if isinstance(reason, str) and len(reason) > _MAX_REASON_LENGTH:
            raise CliServerRestartError(400, f"reason must be at most {_MAX_REASON_LENGTH} characters")
        mode, supervisor = self._resolve_mode()
        if mode is None:
            raise CliServerRestartError(409, RESTART_NOT_SUPPORTED)
        protected = bool(self._services.is_protected()) if self._services.is_protected is not None else False
        if request.actor is None and not protected and not self._options.allow_anonymous:
            raise CliServerRestartError(
                403, "restart needs sign-in: protect the server (users plugin) or set restart.allow_anonymous",
            )
        if self._restarting:
            raise CliServerRestartError(409, "restart already in progress")
        self._restarting = True

        by = request.actor.user_name if request.actor is not None else "anonymous"
        pending = list(self._items.keys())
        text = reason.strip() if isinstance(reason, str) and reason.strip() else None
        logger.info("Restarting the server (%s mode), asked by %s%s", mode, by, f": {text}" if text else "")
        await self._audit(text, by, pending, mode, supervisor, request)
        bus = self._services.event_bus() if self._services.event_bus is not None else None
        if bus is not None:
            data: dict[str, Any] = {"reason": text} if text else {}
            data.update({"by": by, "pending": pending, "mode": mode})
            try:
                await _maybe_await(bus.publish(CLI_SERVER_RESTARTING_EVENT, data, CliEventAudience(permission=_ADMIN_ACCESS)))
            except Exception as exc:  # noqa: BLE001
                logger.error("Publishing %s failed: %s", CLI_SERVER_RESTARTING_EVENT, exc)
        context = CliServerRestartContext(pending=pending, by=by, reason=text)
        # In the background, after the caller answered (its response is an in-flight request the shutdown waits for)
        self._completion = asyncio.ensure_future(self._run(mode, context))
        return {"restarting": True, "pending": pending}

    async def _audit(
        self, reason: Optional[str], by: str, pending: list[str], mode: str, supervisor: Optional[str],
        request: CliServerRestartRequest,
    ) -> None:
        sink = self._services.audit() if self._services.audit is not None else None
        if sink is None:
            return
        properties: dict[str, Any] = {"reason": reason} if reason else {}
        properties.update({"by": by, "pending": pending, "mode": mode})
        if supervisor:
            properties["supervisor"] = supervisor
        if request.via:
            properties["via"] = request.via
        user_id = request.actor.user_id if request.actor is not None else None
        try:
            await _maybe_await(sink(AuditEntry(
                timestamp=int(time.time() * 1000),
                event=CLI_SERVER_RESTART_AUDIT_EVENT,
                username=by,
                user_id=user_id or None,
                actor_id=user_id or None,
                actor=by,
                target="server",
                details=reason or (f"pending: {', '.join(pending)}" if pending else "restart"),
                properties=properties,
            )))
        except Exception as exc:  # noqa: BLE001 - the restart goes on; the audit failing is logged
            logger.error("Auditing the restart failed: %s", exc)

    async def _run(self, mode: str, context: CliServerRestartContext) -> None:
        """Stops the server gracefully, then the mode's action.  Under uvicorn: it stops accepting and finishes the
        in-flight requests (up to the drain timeout), and its lifespan shutdown finishes the restart
        (:meth:`server_stopped`); without one, the service shuts down on its own."""
        await asyncio.sleep(0)
        try:
            server = self._server if self._server is not None else self._runtime.find_server()
            if server is not None:
                self._action = (mode, context)
                config = getattr(server, "config", None)
                if config is not None and getattr(config, "timeout_graceful_shutdown", None) is None:
                    config.timeout_graceful_shutdown = self._options.drain_timeout
                server.should_exit = True
                return
            await self.shutdown()
            await self._act(mode, context)
        except Exception as exc:  # noqa: BLE001
            logger.error("Restart failed: %s", exc)

    async def server_stopped(self) -> None:
        """Called by the app's lifespan shutdown: when the server stopped for a restart, closes what is left, runs the
        shutdown hooks and the mode's action.  Nothing on any other shutdown."""
        action, self._action = self._action, None
        if action is None:
            return
        try:
            await self.shutdown(drain=False)
            await self._act(*action)
        except Exception as exc:  # noqa: BLE001
            logger.error("Restart failed: %s", exc)

    async def _act(self, mode: str, context: CliServerRestartContext) -> None:
        if mode == "exit":
            code = self._options.exit_code
            logger.info("Exiting with code %d for the supervisor to start the server again", code)
            self._runtime.exit(code)
            return
        if mode == "self":
            self._respawn()
            return
        handler = self._options.handler
        if handler is not None:
            await _maybe_await(handler(context))

    def _respawn(self) -> None:
        """Runs the same command again in this process (same executable, arguments, environment and working
        directory); exits with 1 when it cannot."""
        runtime = self._runtime
        args = [runtime.executable, *runtime.argv]
        try:
            logger.info("Starting the server again: %s", " ".join(args))
            runtime.exec(runtime.executable, args, runtime.env, runtime.cwd())
        except Exception as exc:  # noqa: BLE001
            logger.error("Starting the server again failed: %s", exc)
            runtime.exit(1)

    # ------------------------------------------------------------------ graceful shutdown

    async def track(self, scope: dict, receive: Any, send: Any, app: Any) -> None:
        """Runs an HTTP request counting it in flight (the server's middleware), so the shutdown waits for it."""
        self._in_flight += 1
        try:
            await app(scope, receive, send)
        finally:
            self._in_flight -= 1

    async def shutdown(self, drain: bool = True) -> None:
        """Waits for the in-flight requests (up to the drain timeout), closes every WebSocket with 1012, and runs the
        shutdown hooks."""
        if drain:
            deadline = time.monotonic() + self._options.drain_timeout
            while self._in_flight > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.025)
            if self._in_flight > 0:
                logger.warning("%d request(s) still running after the drain timeout", self._in_flight)
        if self._services.close_sockets is not None:
            try:
                await self._services.close_sockets(CLI_SERVER_RESTART_CLOSE_CODE, "Server restarting")
            except Exception as exc:  # noqa: BLE001
                logger.error("Closing the WebSockets failed: %s", exc)
        for name, fn in list(self._hooks):
            try:
                await asyncio.wait_for(_maybe_await_call(fn), _HOOK_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                logger.error("Shutdown step %s failed: %s", name, exc or type(exc).__name__)


async def _maybe_await_call(fn: Callable[[], Any]) -> Any:
    return await _maybe_await(fn())


class CliRestartRequestTracker:
    """ASGI middleware counting the HTTP requests in flight for a restart's graceful shutdown."""

    def __init__(self, app: Any, restart: CliServerRestart) -> None:
        self.app = app
        self.restart = restart

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        await self.restart.track(scope, receive, send, self.app)


__all__ = [
    "DEFAULT_RESTART_DRAIN_TIMEOUT",
    "DEFAULT_RESTART_EXIT_CODE",
    "RESTART_NOT_SUPPORTED",
    "CliRestartRequestTracker",
    "CliServerRestart",
    "CliServerRestartContext",
    "CliServerRestartOptions",
    "CliServerRestartRuntime",
    "CliServerRestartServices",
    "detect_supervisor",
]
