"""``admin restart [--yes] [--reason <text>]`` and ``admin restart status [--json]``: restarting the server from the
terminal, and the changes waiting for a restart."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from qodalis_cli_server_abstractions import (
    CliCommandExample,
    CliCommandParameterDescriptor,
    CliCommandProcessor,
    CliProcessCommand,
    ICliCommandProcessor,
)
from qodalis_cli_server_abstractions.config import has_cli_permission
from qodalis_cli_server_abstractions.restart import (
    CLI_SERVER_RESTART_PERMISSION,
    CliServerRestartActor,
    CliServerRestartError,
    CliServerRestartRequest,
)

_JSON = CliCommandParameterDescriptor("json", "Print the result as JSON", False, "boolean")


def _short_time(iso: str) -> str:
    """``2026-11-25T10:04:05.123Z`` -> ``2026-11-25 10:04:05``."""
    return re.sub(r"(\.\d+)?Z$", "", iso.replace("T", " ", 1))


def _caller() -> Any:
    try:
        from qodalis_cli import get_cli_caller  # the server package, optional here
    except ImportError:  # pragma: no cover
        return None
    return get_cli_caller()


def _pending_lines(status: dict[str, Any]) -> list[str]:
    """The pending changes as lines: ``  - CORS: Mode changed (config, since 2026-11-25 10:04:05)``."""
    pending = status["pending"]
    if not pending:
        return ["No changes are waiting for a restart."]
    return [
        f"{len(pending)} change(s) take effect after a restart:",
        *(f"  - {p['description']} ({p['source']}, since {_short_time(p['since'])})" for p in pending),
    ]


def _mode_line(status: dict[str, Any]) -> str:
    """How the server restarts, in one line."""
    if not status["supported"]:
        return f"Restart from here: not supported ({status.get('reason') or 'no mode'})"
    mode = status["mode"]
    if mode == "exit":
        how = f"exits for {status.get('supervisor') or 'the supervisor'} to start it again"
    elif mode == "self":
        how = "starts itself again"
    else:
        how = "the host's handler"
    return f"Restart mode: {mode} ({how})"


def _flag(command: CliProcessCommand, *names: str) -> bool:
    args = command.args or {}
    return any(args.get(n) is True or (isinstance(args.get(n), str) and args[n].lower() in ("true", "1", "")) for n in names)


class AdminRestartStatusProcessor(CliCommandProcessor):
    command = "status"
    description = "Whether the server can restart itself, how, since when it runs, and the changes waiting for a restart"
    parameters = [_JSON]

    def __init__(self, restart: Any) -> None:
        self._restart = restart

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        status = self._restart.status()
        if _flag(command, "json"):
            return json.dumps(status, indent=2, ensure_ascii=False)
        return "\n".join([f"Running since {_short_time(status['startedAt'])}", _mode_line(status), *_pending_lines(status)])


class AdminRestartProcessor(CliCommandProcessor):
    """``admin restart [--yes] [--reason <text>]``: restarts the server (``server:restart``).  Server commands cannot
    ask, so without ``--yes`` it prints what is pending and how to confirm.  ``admin restart status`` needs only
    ``admin:access``."""

    command = "restart"
    description = "Restarts the server (needs --yes and server:restart); restart status shows the changes waiting for it"
    parameters = [
        CliCommandParameterDescriptor("yes", "Restart now (without it, only shows what is pending)", False, "boolean", ["y"]),
        CliCommandParameterDescriptor("reason", "Why, kept in the audit log", False, "string"),
    ]
    examples = [
        CliCommandExample("admin restart", "What is pending, and how to confirm"),
        CliCommandExample('admin restart --yes --reason "apply CORS"', "Restart now"),
        CliCommandExample("admin restart status", "Mode, supervisor, start time, pending changes"),
    ]

    def __init__(self, restart: Any) -> None:
        self._restart = restart
        self._processors: list[ICliCommandProcessor] = [AdminRestartStatusProcessor(restart)]

    @property
    def processors(self) -> list[ICliCommandProcessor]:
        return self._processors

    async def handle_async(self, command: CliProcessCommand, cancellation_event: asyncio.Event | None = None) -> str:
        status = self._restart.status()
        caller = _caller()
        # Checked here, not as required_permissions: ``admin restart status`` needs only admin:access
        if caller is not None and not has_cli_permission(caller.permissions, CLI_SERVER_RESTART_PERMISSION):
            return f"Error: forbidden: missing permission {CLI_SERVER_RESTART_PERMISSION}"
        if not status["supported"]:
            return "\n".join([f"Error: restart not supported: {status.get('reason')}", *_pending_lines(status)])
        if not _flag(command, "yes", "y"):
            return "\n".join([
                *_pending_lines(status), _mode_line(status),
                'Run admin restart --yes [--reason "<why>"] to restart the server now.',
            ])
        reason = (command.args or {}).get("reason")
        try:
            result = await self._restart.restart(CliServerRestartRequest(
                reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
                actor=CliServerRestartActor(user_name=caller.user_name, user_id=caller.user_id) if caller is not None else None,
                via="command",
            ))
        except CliServerRestartError as exc:
            return f"Error: {exc.message}"
        applied = f" to apply {len(result['pending'])} pending change(s)" if result["pending"] else ""
        return (
            f"Restarting the server{applied} ({status['mode']} mode). "
            "The connection drops and comes back once it runs again."
        )
