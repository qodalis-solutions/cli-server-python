#: The package's icon on the admin dashboard's plugins page (``CliInstalledPlugin.icon``).
__plugin_icon__ = "🧱"

from .cli_command_author import ICliCommandAuthor, CliCommandAuthor, DEFAULT_LIBRARY_AUTHOR
from .cli_process_command import CliProcessCommand
from .shell_words import split_shell_words, join_shell_words, get_positionals, get_positional_text
from .cli_command_parameter_descriptor import (
    ICliCommandParameterDescriptor,
    CliCommandParameterDescriptor,
)
from .cli_command_processor import (
    CliCommandExample,
    CliCommandProcessor,
    CliCommandRefusal,
    ICliCommandProcessor,
    command_matches,
)
from .cli_paging import CliPageRequest, CliPaging, DEFAULT_TAKE, MAX_TAKE
from .cli_module import ICliModule, CliModule
from .cli_processor_filter import ICliProcessorFilter
from .events import (
    CliEvent,
    CliEventAudience,
    CliEventHandler,
    ICliEventBus,
    cli_event_pattern_matches,
)
from .audit import AuditEntry, AuditMetadata, AuditQueryOptions, IAuditStore
from .jobs import (
    ICliJob,
    ICliJobExecutionContext,
    ICliJobLogger,
    CliJobOptions,
    ICliJobStorageProvider,
    JobExecution,
    JobExecutionQuery,
    JobExecutionSummary,
    JobState,
    JobLogEntry,
)
from .data_explorer_types import (
    DataExplorerLanguage,
    DataExplorerOutputFormat,
    DataExplorerTemplate,
    DataExplorerParameterDescriptor,
    DataExplorerProviderOptions,
    DataExplorerExecutionContext,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaTable,
    DataExplorerSchemaResult,
    DataExplorerRateLimit,
    DataExplorerCaller,
    DataExplorerAuditEntry,
    DataExplorerErrorCode,
    is_read_only_source,
)
from .data_explorer_provider import IDataExplorerProvider
from .cli_stream_command_processor import ICliStreamCommandProcessor, is_stream_capable
from .table_names import (
    DEFAULT_TABLE_PREFIX,
    CliTableNameOptions,
    is_sql_identifier,
    qcli_table_name,
)
from .route_permissions import CliRoutePermission
from .config import *  # noqa: F401,F403 (config providers)
from .config import __all__ as _config_all
from .restart import *  # noqa: F401,F403 (restarting the server)
from .restart import __all__ as _restart_all
from .plugin_entry import *  # noqa: F401,F403 (server plugins installed at runtime)
from .plugin_entry import __all__ as _plugin_entry_all
from .notifications import *  # noqa: F401,F403 (system notifications)
from .notifications import __all__ as _notifications_all

__all__ = [
    *_config_all,
    *_restart_all,
    *_plugin_entry_all,
    *_notifications_all,
    "DEFAULT_TABLE_PREFIX",
    "CliTableNameOptions",
    "is_sql_identifier",
    "qcli_table_name",
    "CliRoutePermission",
    "ICliCommandAuthor",
    "CliCommandAuthor",
    "DEFAULT_LIBRARY_AUTHOR",
    "CliProcessCommand",
    "CliCommandRefusal",
    "split_shell_words",
    "join_shell_words",
    "get_positionals",
    "get_positional_text",
    "ICliCommandParameterDescriptor",
    "CliCommandParameterDescriptor",
    "ICliCommandProcessor",
    "CliCommandProcessor",
    "CliCommandExample",
    "command_matches",
    "CliPageRequest",
    "CliPaging",
    "DEFAULT_TAKE",
    "MAX_TAKE",
    "ICliModule",
    "CliModule",
    "ICliProcessorFilter",
    "CliEvent",
    "CliEventAudience",
    "CliEventHandler",
    "ICliEventBus",
    "cli_event_pattern_matches",
    "AuditEntry",
    "AuditMetadata",
    "AuditQueryOptions",
    "IAuditStore",
    "ICliJob",
    "ICliJobExecutionContext",
    "ICliJobLogger",
    "CliJobOptions",
    "ICliJobStorageProvider",
    "JobExecution",
    "JobExecutionQuery",
    "JobExecutionSummary",
    "JobState",
    "JobLogEntry",
    "DataExplorerLanguage",
    "DataExplorerOutputFormat",
    "DataExplorerTemplate",
    "DataExplorerParameterDescriptor",
    "DataExplorerProviderOptions",
    "DataExplorerExecutionContext",
    "DataExplorerResult",
    "DataExplorerSchemaColumn",
    "DataExplorerSchemaTable",
    "DataExplorerSchemaResult",
    "DataExplorerRateLimit",
    "DataExplorerCaller",
    "DataExplorerAuditEntry",
    "DataExplorerErrorCode",
    "is_read_only_source",
    "IDataExplorerProvider",
    "ICliStreamCommandProcessor",
    "is_stream_capable",
]
