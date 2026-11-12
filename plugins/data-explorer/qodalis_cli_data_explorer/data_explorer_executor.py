"""Executor that dispatches queries to the correct Data Explorer provider."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerResult,
)

from .data_explorer_policy import (
    DataExplorerRateLimiter,
    apply_column_policy,
    filter_listing,
    filter_schema,
    is_read_only,
    masked_column_error,
    masked_result_error,
    missing_permissions,
    fix_name_case,
    name_case_hint,
    objects_error,
    quote_name_ansi,
    rate_limit_of,
    read_only_error,
)
from .data_explorer_registry import DataExplorerRegistry

try:  # abstractions with data explorer hardening
    from qodalis_cli_server_abstractions import DataExplorerAuditEntry
except ImportError:  # pragma: no cover - older abstractions package
    @dataclass
    class DataExplorerAuditEntry:  # type: ignore[no-redef]
        """An audit entry the data explorer writes (event ``data_query``)."""

        event: str
        user_id: str
        user_name: str
        metadata: dict[str, Any]

logger = logging.getLogger(__name__)

#: Writes an audit entry (the server's audit log).
DataExplorerAuditSink = Callable[[DataExplorerAuditEntry], Awaitable[None]]

#: Longest query text kept in an audit entry.
AUDIT_QUERY_MAX_LENGTH = 2000

#: Most names whose case a query has fixed before it runs again
#: (``DataExplorerProviderOptions.fix_name_case``).
MAX_NAME_FIXES = 5


def result_to_dict(result: DataExplorerResult) -> dict[str, Any]:
    """The result as a snake_case dict; ``error_code`` only when the server
    refused the query and ``executed_query`` only when the server changed it
    (works with older abstractions, whose result has no such fields)."""
    data = asdict(result)
    code = getattr(result, "error_code", None)
    if code:
        data["error_code"] = code
    else:
        data.pop("error_code", None)
    executed = getattr(result, "executed_query", None)
    if executed:
        data["executed_query"] = executed
    else:
        data.pop("executed_query", None)
    return data


class DataExplorerExecutor:
    """Executes data explorer queries: the source's permissions, rate limit,
    read-only rules, allowed objects and masked columns first, then the provider with a
    timeout, then the row limit, column masking and the audit entry."""

    def __init__(
        self,
        registry: DataExplorerRegistry,
        *,
        audit: Optional[Callable[[], Optional[DataExplorerAuditSink]]] = None,
        rate_limiter: DataExplorerRateLimiter | None = None,
    ) -> None:
        self._registry = registry
        #: Read on every query, so an audit log mounted later counts.
        self._audit = audit
        self._rate_limiter = rate_limiter or DataExplorerRateLimiter()

    async def execute_async(self, request: dict[str, Any], caller: Any = None) -> DataExplorerResult:
        """Execute a query against the named source as ``caller``.

        ``caller`` (a ``DataExplorerCaller``: permissions, rate limit, audit)
        is ``None`` for trusted code.  Never raises: errors (and refusals,
        with ``error_code``) are in the result."""
        source = request.get("source", "")
        query = request.get("query", "") or ""
        parameters = request.get("parameters") or {}

        entry = self._registry.get(source)
        if entry is None:
            return _failure(source, None, f"Unknown data source: {source}", 0, "unknown_source")

        provider, options = entry
        start = time.monotonic()

        missing = missing_permissions(getattr(options, "required_permissions", None), caller)
        if missing:
            return await self._refuse(request, options, caller, f"forbidden: missing permission {', '.join(missing)}", "forbidden")
        if not self._rate_limiter.try_acquire(options, caller):
            max_queries, window = rate_limit_of(options)  # type: ignore[misc]
            return await self._refuse(
                request, options, caller, f"Too many queries: at most {max_queries} per {window:g} s", "rate_limited",
            )
        sent = query
        for fixes in range(MAX_NAME_FIXES + 1):
            if is_read_only(options) and not getattr(provider, "read_only_queries", False):
                error = read_only_error(options.language, query)
                if error:
                    return await self._refuse(request, options, caller, error, "read_only")
            objects = objects_error(options, query)
            if objects:
                return await self._refuse(request, options, caller, objects, "object_not_allowed")
            masked = masked_column_error(options, query)
            if masked:
                return await self._refuse(request, options, caller, masked, "masked_column")

            context = DataExplorerExecutionContext(query=query, parameters=parameters, options=options)
            timeout_s = options.timeout / 1000.0
            try:
                result = await asyncio.wait_for(provider.execute_async(context), timeout=timeout_s)
            except asyncio.TimeoutError:
                result = _failure(source, options, f"Query timed out after {options.timeout}ms", options.timeout)
            except Exception as exc:
                error = str(exc)
                schema = await self._visible_schema(provider, options) if _language_of(options) == "sql" else None
                # A read-only query changed nothing, so it runs again with the name's case fixed (one name per run)
                fixed = (
                    fix_name_case(query, error, schema, _quote_of(provider))
                    if is_read_only(options) and getattr(options, "fix_name_case", True) and fixes < MAX_NAME_FIXES
                    else None
                )
                if fixed is not None:
                    logger.info("Data explorer query on source '%s' runs again as: %s", source, fixed)
                    query = fixed
                    continue
                hint = name_case_hint(error, schema)
                result = _failure(source, options, error if hint is None else f"{error.rstrip()}\n{hint}", _elapsed_ms(start))
            else:
                if query != sent:
                    result.executed_query = query
                if result.row_count > options.max_rows:
                    result.rows = result.rows[: options.max_rows]
                    result.row_count = options.max_rows
                    result.truncated = True
                masked = masked_result_error(options, query, result.columns) if result.success else None
                if masked:
                    # A masked column came back under another name: withhold the rows
                    result = _failure(source, options, masked, _elapsed_ms(start), "masked_column")
                elif result.success:
                    filter_listing(result, options, query)
                    apply_column_policy(result, options)
            break

        await self._write_audit(request, options, caller, result, _elapsed_ms(start))
        return result

    async def _refuse(self, request: dict[str, Any], options: Any, caller: Any, error: str, code: str) -> DataExplorerResult:
        result = _failure(request.get("source", ""), options, error, 0, code)
        await self._write_audit(request, options, caller, result, 0)
        return result

    async def _visible_schema(self, provider: Any, options: Any) -> Any:
        """The source's schema as its callers see it, or ``None`` when the
        provider cannot give one."""
        try:
            schema = await provider.get_schema_async(options)
            return filter_schema(schema, options) if schema is not None else None
        except Exception:
            logger.debug("No schema for the name hint on source '%s'", getattr(options, "name", ""), exc_info=True)
            return None

    async def _write_audit(self, request: dict[str, Any], options: Any, caller: Any, result: DataExplorerResult, duration_ms: int) -> None:
        """Writes the ``data_query`` audit entry; a failing audit log does not fail the query."""
        if getattr(options, "audit", True) is False or self._audit is None:
            return
        try:
            sink = self._audit()
            if sink is None:
                return
            metadata: dict[str, Any] = {
                "source": options.name,
                "query": _truncate(request.get("query", "") or ""),
                "success": result.success,
                "rowCount": result.row_count,
                "durationMs": duration_ms,
            }
            if result.error:
                metadata["error"] = result.error
            executed = getattr(result, "executed_query", None)
            if executed:
                metadata["executedQuery"] = _truncate(executed)
            remote = getattr(caller, "remote_address", None) if caller is not None else None
            if remote:
                metadata["remoteAddress"] = remote
            await sink(DataExplorerAuditEntry(
                event="data_query",
                user_id=(getattr(caller, "user_id", None) if caller is not None else None) or "",
                user_name=(getattr(caller, "user_name", None) if caller is not None else None) or "anonymous",
                metadata=metadata,
            ))
        except Exception:
            logger.exception("Failed to write the data_query audit entry")


def _truncate(text: str) -> str:
    return text[:AUDIT_QUERY_MAX_LENGTH] + "…" if len(text) > AUDIT_QUERY_MAX_LENGTH else text


def _quote_of(provider: Any) -> Callable[[str], str]:
    """The provider's ``quote_name`` (older abstractions have none: ANSI)."""
    quote = getattr(provider, "quote_name", None)
    return quote if callable(quote) else quote_name_ansi


def _language_of(options: Any) -> str:
    language = getattr(options, "language", "sql")
    return str(getattr(language, "value", language))


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _failure(source: str, options: Any, error: str, execution_time: int, error_code: str | None = None) -> DataExplorerResult:
    result = DataExplorerResult(
        success=False,
        source=source,
        language=options.language if options is not None else "sql",  # type: ignore[arg-type]
        default_output_format=options.default_output_format if options is not None else "table",  # type: ignore[arg-type]
        execution_time=execution_time,
        columns=None,
        rows=[],
        row_count=0,
        truncated=False,
        error=error,
    )
    if error_code:
        # Set as an attribute: older abstractions have no such field
        result.error_code = error_code  # type: ignore[attr-defined]
    return result
