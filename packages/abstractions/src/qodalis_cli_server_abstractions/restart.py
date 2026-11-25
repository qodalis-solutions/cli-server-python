"""Restarting the server from the dashboard, the ``admin restart`` command or the API
(``/api/v1/qcli/server/restart``), and the list of changes waiting for a restart.  The server implements
:class:`ICliServerRestart` (``builder.restart``); plugins mark their own pending changes with ``mark_required``.
The same contract and JSON as the Node and .NET servers."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Literal, Optional

#: The permission ``POST /api/v1/qcli/server/restart`` and ``admin restart`` need.
CLI_SERVER_RESTART_PERMISSION = "server:restart"
#: Published when the pending list changes (``{ pending }``, audience ``admin:access``).
CLI_SERVER_RESTART_REQUIRED_EVENT = "server:restart-required"
#: Published when a restart starts (``{ reason, by, pending, mode }``, audience ``admin:access``).
CLI_SERVER_RESTARTING_EVENT = "server:restarting"
#: The audit event of a restart.
CLI_SERVER_RESTART_AUDIT_EVENT = "server.restart"
#: The WebSocket close code sent to every socket when the server restarts (RFC 6455: service restart).
CLI_SERVER_RESTART_CLOSE_CODE = 1012

#: How the server restarts:
#:
#: - ``exit``: graceful shutdown, then exit with ``exit_code`` (default 75) for a supervisor to start it again
#:   (Docker restart policy, systemd ``Restart=always``, PM2, Kubernetes);
#: - ``self``: graceful shutdown, then the same command runs again in this process (``sys.executable`` with the same
#:   arguments, environment and working directory): bare runs and development;
#: - ``handler``: graceful shutdown, then the host's function;
#: - ``auto`` (default): ``exit`` when a supervisor is detected, otherwise restarting is not supported.
CliServerRestartMode = Literal["auto", "exit", "self", "handler"]
CLI_SERVER_RESTART_MODES: tuple[str, ...] = ("auto", "exit", "self", "handler")

#: A supervisor that starts the process again when it exits.
CliServerSupervisor = Literal["docker", "systemd", "pm2", "kubernetes"]


@dataclass(frozen=True)
class CliRestartRequirement:
    """A change that takes effect only after a restart."""

    #: Unique key: marking the same id again keeps the first entry (e.g. ``config:cors.cors.mode``).
    id: str
    #: Who marked it: ``config``, a plugin's id, ...
    source: str
    #: One line for people: ``CORS: Mode changed``.
    description: str
    #: When it was marked (ISO 8601); set by the server.
    since: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "description": self.description, "since": self.since}


@dataclass(frozen=True)
class CliServerRestartActor:
    """The signed-in caller asking for a restart."""

    user_name: str
    user_id: Optional[str] = None


@dataclass(frozen=True)
class CliServerRestartRequest:
    """Who asks for a restart, and why."""

    #: Free text kept in the audit log (at most 500 characters).
    reason: Any = None
    #: The signed-in caller; ``None`` on a server without sign-in.
    actor: Optional[CliServerRestartActor] = None
    #: Where it came from: ``api``, ``command``, ... (audited).
    via: Optional[str] = None


class CliServerRestartError(Exception):
    """A refused restart: 409 not supported or already restarting, 403 without sign-in, 400 bad input."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ICliServerRestart(abc.ABC):
    """The server's restart service (``builder.restart`` / ``create_cli_server().restart``)."""

    #: When this process started serving (ISO 8601).
    started_at: str

    @property
    @abc.abstractmethod
    def restarting(self) -> bool:
        """True once a restart was accepted."""

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """``GET /api/v1/qcli/server/restart``: ``{ supported, mode, supervisor, reason?, startedAt, pending }``."""

    @abc.abstractmethod
    def pending(self) -> list[CliRestartRequirement]:
        """The pending changes, oldest first; empty after every start."""

    @abc.abstractmethod
    def mark_required(self, id: str, source: str, description: str) -> None:
        """Adds a pending change (nothing happens when the id is already pending); publishes ``server:restart-required``."""

    @abc.abstractmethod
    def clear_required(self, id: str) -> bool:
        """Removes a pending change (e.g. a setting set back to its value at start); True when it was pending."""

    @abc.abstractmethod
    async def restart(self, request: Optional[CliServerRestartRequest] = None) -> dict[str, Any]:
        """Accepts a restart: audits it, publishes ``server:restarting`` and returns ``{ restarting: True, pending }``;
        the graceful shutdown and the mode's action follow in the background.  Raises :class:`CliServerRestartError`."""


__all__ = [
    "CLI_SERVER_RESTART_AUDIT_EVENT",
    "CLI_SERVER_RESTART_CLOSE_CODE",
    "CLI_SERVER_RESTART_MODES",
    "CLI_SERVER_RESTART_PERMISSION",
    "CLI_SERVER_RESTART_REQUIRED_EVENT",
    "CLI_SERVER_RESTARTING_EVENT",
    "CliRestartRequirement",
    "CliServerRestartActor",
    "CliServerRestartError",
    "CliServerRestartMode",
    "CliServerRestartRequest",
    "CliServerSupervisor",
    "ICliServerRestart",
]
