"""Protocol for filtering whether a command processor is allowed to execute."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli_command_processor import ICliCommandProcessor


class ICliProcessorFilter(abc.ABC):
    """Provides a mechanism to filter whether a command processor is allowed to execute.

    Implementations can use this to disable processors at runtime
    (e.g., when a plugin module is toggled off via the admin dashboard).
    """

    @abc.abstractmethod
    def is_allowed(self, processor: ICliCommandProcessor) -> bool:
        """Determine whether the given command processor is allowed to execute.

        Args:
            processor: The command processor to check.

        Returns:
            ``True`` if the processor is allowed; ``False`` if it should be blocked.
        """
        ...
