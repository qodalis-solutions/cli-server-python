from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cli_command_parameter_descriptor import CliCommandParameterDescriptor
from .cli_process_command import CliProcessCommand

#: Rows to show when the command line does not say (``--take``).
DEFAULT_TAKE = 50

#: The largest ``--take`` accepted; larger values are lowered to it.
MAX_TAKE = 500


@dataclass(frozen=True)
class CliPageRequest:
    """The rows a list command was asked for, read from ``--skip`` and
    ``--take``.

    Commands that can return many rows declare ``CliPaging.parameters()``,
    read the request with ``CliPaging.from_command(command)``, query with
    ``skip`` and ``take``, and write the rows with ``write_paged_table`` so the
    terminal shows which rows these are and how to get the next ones.
    """

    skip: int
    """Rows to skip before the first one shown (0 = from the start)."""
    take: int
    """Rows to show (1 to ``MAX_TAKE``)."""


class CliPaging:
    """The ``--skip`` / ``--take`` convention shared by list commands."""

    DEFAULT_TAKE = DEFAULT_TAKE
    MAX_TAKE = MAX_TAKE

    @staticmethod
    def parameters(default_take: int = DEFAULT_TAKE) -> list[CliCommandParameterDescriptor]:
        """The ``--skip`` and ``--take`` parameters, to add to a command's
        ``parameters`` so help and completion show them."""
        return [
            CliCommandParameterDescriptor(
                name="skip",
                description="Rows to skip",
                type="number",
                default_value=0,
            ),
            CliCommandParameterDescriptor(
                name="take",
                description=f"Rows to show (at most {MAX_TAKE})",
                type="number",
                default_value=default_take,
            ),
        ]

    @staticmethod
    def from_command(command: CliProcessCommand, default_take: int = DEFAULT_TAKE) -> CliPageRequest:
        """The rows asked for by ``command``: ``--skip`` (default 0; negative
        values become 0) and ``--take`` (default ``default_take``, at most
        ``MAX_TAKE``; zero or negative values fall back to the default).
        Missing or non-integer values fall back to the defaults."""
        if command is None:
            raise ValueError("command is required")
        skip = _read_int(command, "skip")
        take = _read_int(command, "take")
        return CliPageRequest(
            skip=max(skip, 0) if skip is not None else 0,
            take=min(take if take is not None and take > 0 else default_take, MAX_TAKE),
        )


def _read_int(command: CliProcessCommand, name: str) -> int | None:
    value: Any = (command.args or {}).get(name)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
