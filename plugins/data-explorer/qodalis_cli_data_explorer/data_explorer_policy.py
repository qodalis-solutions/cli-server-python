"""The source options that keep data from leaking.

Spec: web-cli ``docs/superpowers/specs/2026-11-12-data-explorer-hardening-design.md``.

Options are read with ``getattr`` and defaults, so this package works with
older versions of the abstractions package (whose options have no such fields).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

MASK = "***"


# ── Options ──────────────────────────────────────────────────────────


def _language(options: Any) -> str:
    language = getattr(options, "language", "sql")
    return str(getattr(language, "value", language))


def is_read_only(options: Any) -> bool:
    """Whether a source only reads (``read_only`` defaults to ``True``)."""
    return getattr(options, "read_only", True) is not False


def _patterns(options: Any, name: str) -> list[str]:
    return list(getattr(options, name, None) or [])


def rate_limit_of(options: Any) -> tuple[int, float] | None:
    """``(max_queries, window_seconds)`` of the source, or ``None``.  Accepts
    a ``DataExplorerRateLimit`` or a dict (snake_case or camelCase keys)."""
    limit = getattr(options, "rate_limit", None)
    if limit is None:
        return None
    if isinstance(limit, dict):
        max_queries = limit.get("max_queries", limit.get("maxQueries"))
        window = limit.get("window_seconds", limit.get("windowSeconds"))
    else:
        max_queries = getattr(limit, "max_queries", None)
        window = getattr(limit, "window_seconds", None)
    if not max_queries or not window or max_queries <= 0 or window <= 0:
        return None
    return int(max_queries), float(window)


# ── Globs and permissions ────────────────────────────────────────────


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """A glob (``*`` matches anything) as a case-insensitive regular expression."""
    escaped = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(f"^{escaped}$", re.IGNORECASE | re.DOTALL)


def matches_any(name: str, patterns: Sequence[str] | None) -> bool:
    """Whether ``name`` matches one of the glob ``patterns``."""
    return any(glob_to_regex(p).match(name) for p in patterns or [])


def missing_permissions(permissions: Sequence[str] | None, caller: Any) -> list[str]:
    """The ``permissions`` the caller lacks (none on an open server, i.e.
    without a caller or without ``has_permission``)."""
    has_permission = getattr(caller, "has_permission", None) if caller is not None else None
    if not permissions or has_permission is None:
        return []
    return [p for p in permissions if not has_permission(p)]


def can_use_source(options: Any, caller: Any) -> bool:
    """Whether the caller may use the source."""
    return not missing_permissions(_patterns(options, "required_permissions"), caller)


def can_export(options: Any, caller: Any) -> bool:
    """Whether the caller may export results of the source (advisory)."""
    return not missing_permissions(_patterns(options, "export_permissions"), caller)


# ── SQL tokens ───────────────────────────────────────────────────────


@dataclass
class SqlToken:
    kind: str  # "word" | "ident" | "punct"
    #: Upper-case for words, as written for quoted identifiers.
    value: str
    #: As written (words keep their case for object names).
    raw: str


_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def tokenize_sql(sql: str) -> list[SqlToken]:
    """Splits SQL into words, quoted identifiers and punctuation; comments and
    string literals are dropped, so keywords inside them do not count."""
    tokens: list[SqlToken] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if (c == "-" and nxt == "-") or c == "#":
            # line comment (# is MySQL's)
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                elif sql[i] == "\\":
                    i += 2
                elif sql[i] == "'":
                    break
                else:
                    i += 1
            i += 1
            continue
        if c == "$":
            # Postgres dollar-quoted string: $tag$ ... $tag$
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end < 0 else end + len(tag)
                continue
        if c in ('"', "`", "["):
            close = "]" if c == "[" else c
            end = sql.find(close, i + 1)
            name = sql[i + 1 : n if end < 0 else end]
            tokens.append(SqlToken("ident", name, name))
            i = n if end < 0 else end + 1
            continue
        if c.isascii() and (c.isalpha() or c == "_"):
            j = i + 1
            while j < n and sql[j].isascii() and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            raw = sql[i:j]
            tokens.append(SqlToken("word", raw.upper(), raw))
            i = j
            continue
        if c.isspace() or c.isdigit():
            i += 1
            continue
        tokens.append(SqlToken("punct", c, c))
        i += 1
    return tokens


_SQL_READ_STARTS = {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC", "VALUES", "TABLE"}
_SQL_WRITE_WORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
    "REVOKE", "COPY", "CALL", "EXEC", "EXECUTE", "INTO", "LOCK", "VACUUM", "ATTACH", "DETACH", "PRAGMA", "SET",
}
#: Write keywords that are also read-only functions when a ``(`` follows
#: (``REPLACE(name, 'a', 'b')``).
_SQL_FUNCTION_WORDS = {"REPLACE", "INSERT"}


def _sql_read_only_error(query: str) -> str | None:
    statements: list[list[SqlToken]] = [[]]
    for t in tokenize_sql(query):
        if t.kind == "punct" and t.value == ";":
            statements.append([])
        else:
            statements[-1].append(t)
    non_empty = [s for s in statements if s]
    if len(non_empty) != 1:
        return "Read-only source: run one statement at a time"
    statement = non_empty[0]
    first = statement[0]
    if first.kind != "word" or first.value not in _SQL_READ_STARTS:
        return "Read-only source: only SELECT, WITH, EXPLAIN, SHOW, DESCRIBE, VALUES or TABLE queries are allowed"
    for i, t in enumerate(statement):
        if t.kind != "word" or t.value not in _SQL_WRITE_WORDS:
            continue
        if t.value in _SQL_FUNCTION_WORDS and i + 1 < len(statement) and statement[i + 1].value == "(":
            continue
        return f"Read-only source: {t.value} is not allowed"
    return None


#: Functions whose arguments use FROM without naming a table (``EXTRACT(YEAR FROM day)``).
_SQL_FROM_FUNCTIONS = {"EXTRACT", "SUBSTRING", "SUBSTR", "TRIM", "POSITION", "OVERLAY"}
_SQL_OBJECT_KEYWORDS = {"FROM", "JOIN", "INTO", "UPDATE", "TABLE"}
_SQL_CLAUSE_WORDS = {
    "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "ON", "USING", "GROUP", "ORDER", "HAVING",
    "LIMIT", "OFFSET", "UNION", "EXCEPT", "INTERSECT", "WINDOW", "FETCH", "FOR", "NATURAL", "SET", "VALUES",
    "SELECT", "RETURNING", "TABLESAMPLE", "WITH",
}


def sql_objects(query: str) -> list[str]:
    """The tables a SQL query names (after FROM, JOIN, INTO, UPDATE, TABLE),
    without the CTE names it defines."""
    tokens = tokenize_sql(query)

    def at(k: int) -> SqlToken | None:
        return tokens[k] if 0 <= k < len(tokens) else None

    def is_name(t: SqlToken | None) -> bool:
        return t is not None and t.kind in ("ident", "word")

    def value(k: int) -> str | None:
        t = at(k)
        return t.value if t is not None else None

    # CTE names: name [ (columns) ] AS (
    cte_names: set[str] = set()
    for i, tok in enumerate(tokens):
        if not is_name(tok):
            continue
        j = i + 1
        if value(j) == "(":
            depth = 0
            while j < len(tokens):
                if tokens[j].value == "(":
                    depth += 1
                elif tokens[j].value == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            j += 1
        if value(j) == "AS" and value(j + 1) == "(":
            cte_names.add(tok.raw.lower())

    def read_name(start: int) -> tuple[str, int] | None:
        if not is_name(at(start)):
            return None
        parts = [tokens[start].raw]
        k = start + 1
        while value(k) == "." and is_name(at(k + 1)):
            parts.append(tokens[k + 1].raw)
            k += 2
        return ".".join(parts), k

    objects: list[str] = []
    openers: list[str | None] = []
    for i, t in enumerate(tokens):
        if t.value == "(" and t.kind == "punct":
            prev = at(i - 1)
            openers.append(prev.value if prev is not None and prev.kind == "word" else None)
            continue
        if t.value == ")" and t.kind == "punct":
            if openers:
                openers.pop()
            continue
        if t.kind != "word" or t.value not in _SQL_OBJECT_KEYWORDS:
            continue
        if t.value == "FROM" and openers and openers[-1] in _SQL_FROM_FUNCTIONS:
            continue
        k = i + 1
        # FROM a, b, c
        while True:
            if value(k) in ("ONLY", "LATERAL"):
                k += 1
            name = read_name(k)
            if name is None:
                break
            if name[0].lower() not in cte_names:
                objects.append(name[0])
            k = name[1]
            # An alias: [AS] alias
            nxt = at(k)
            if value(k) == "AS":
                k += 2
            elif nxt is not None and nxt.kind == "word" and nxt.value not in _SQL_CLAUSE_WORDS:
                k += 1
            if value(k) != "," or t.value != "FROM":
                break
            k += 1
    return objects


# ── MongoDB ──────────────────────────────────────────────────────────

_MONGO_READ_METHODS = {"find", "findone", "countdocuments", "estimateddocumentcount", "distinct", "aggregate"}
_MONGO_SHOW = re.compile(r"^show\s+(collections|dbs|databases)\s*;?$", re.IGNORECASE)
_MONGO_CALL = re.compile(
    r"""^db\.(?:getCollection\(\s*['"]([^'"]+)['"]\s*\)|([A-Za-z0-9_$-]+(?:\.[A-Za-z0-9_$-]+)*?))\.(\w+)\s*\("""
)
_MONGO_WRITE_STAGE = re.compile(r"""["']?\$(out|merge)["']?\s*:""")


def _mongo_read_only_error(query: str) -> str | None:
    q = query.strip()
    if _MONGO_SHOW.match(q):
        return None
    m = _MONGO_CALL.match(q)
    if m is None or m.group(3).lower() not in _MONGO_READ_METHODS:
        return ("Read-only source: only find, findOne, countDocuments, estimatedDocumentCount, "
                "distinct and aggregate are allowed")
    if m.group(3).lower() == "aggregate" and _MONGO_WRITE_STAGE.search(q):
        return "Read-only source: $out and $merge are not allowed"
    return None


def _mongo_objects(query: str) -> list[str] | None:
    q = query.strip()
    if _MONGO_SHOW.match(q):
        return []
    m = _MONGO_CALL.match(q)
    return [m.group(1) or m.group(2)] if m else None


# ── Redis ────────────────────────────────────────────────────────────

_REDIS_READ = {
    "GET", "MGET", "STRLEN", "GETRANGE", "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS", "LRANGE",
    "LLEN", "LINDEX", "SMEMBERS", "SCARD", "SISMEMBER", "ZRANGE", "ZRANGEBYSCORE", "ZREVRANGE", "ZCARD", "ZSCORE",
    "TTL", "PTTL", "EXISTS", "TYPE", "KEYS", "SCAN", "INFO", "DBSIZE", "PING",
}
_REDIS_NO_KEYS = {"INFO", "DBSIZE", "PING"}
#: Commands whose every argument is a key.
_REDIS_ALL_KEYS = {"MGET", "EXISTS", "DEL", "UNLINK", "TOUCH"}
_REDIS_WORD = re.compile(r""""[^"]*"|'[^']*'|\S+""")


def _redis_words(query: str) -> list[str]:
    return [re.sub(r"""^["']|["']$""", "", w) for w in _REDIS_WORD.findall(query.strip())]


def _redis_read_only_error(query: str) -> str | None:
    words = _redis_words(query)
    command = words[0].upper() if words else None
    if command and command in _REDIS_READ:
        return None
    return f"Read-only source: {command or 'this command'} is not allowed"


def _redis_objects(query: str) -> list[str] | None:
    """The keys (or key patterns) a Redis command names; ``None`` when it may touch any key."""
    words = _redis_words(query)
    if not words:
        return []
    cmd, args = words[0].upper(), words[1:]
    if cmd in _REDIS_NO_KEYS:
        return []
    if cmd == "SCAN":
        for idx, arg in enumerate(args):
            if arg.upper() == "MATCH":
                return [args[idx + 1]] if idx + 1 < len(args) else None
        return None
    if cmd == "KEYS":
        return [args[0]] if args else None
    if cmd in _REDIS_ALL_KEYS:
        return args
    if cmd == "MSET":
        return args[::2]
    return [args[0]] if args else []


# ── Elasticsearch ────────────────────────────────────────────────────

_ES_READ_POST = re.compile(r"/(_search|_count|_msearch|_mget|_field_caps|_validate/query)$")
_ES_REQUEST_LINE = re.compile(r"^(GET|POST|PUT|DELETE|HEAD|PATCH)\s+(\S+)", re.IGNORECASE)


def _es_request_line(query: str) -> tuple[str, str]:
    first = query.strip().split("\n")[0].strip()
    m = _ES_REQUEST_LINE.match(first)
    if m:
        path = m.group(2)
        return m.group(1).upper(), path if path.startswith("/") else f"/{path}"
    return "GET", first if first.startswith("/") else f"/{first}"


def _es_read_only_error(query: str) -> str | None:
    verb, path = _es_request_line(query)
    bare = path.split("?")[0]
    if verb in ("GET", "HEAD"):
        return None
    if verb == "POST" and _ES_READ_POST.search(bare):
        return None
    return f"Read-only source: {verb} {bare} is not allowed"


def _es_objects(query: str) -> list[str] | None:
    _, path = _es_request_line(query)
    parts = [p for p in path.split("?")[0].split("/") if p]
    if not parts or parts[0].startswith("_"):
        return None
    return parts[0].split(",")


# ── Checks ───────────────────────────────────────────────────────────


def read_only_error(language: Any, query: str) -> str | None:
    """Why a read-only source refuses the query, or ``None`` when it only reads."""
    lang = str(getattr(language, "value", language))
    if lang == "sql":
        return _sql_read_only_error(query)
    if lang in ("shell", "json"):
        # MongoDB sources use either language
        return _mongo_read_only_error(query)
    if lang == "redis":
        return _redis_read_only_error(query)
    if lang == "elasticsearch":
        return _es_read_only_error(query)
    return f"Read-only source: {lang} queries cannot be checked; set read_only=False to allow them"


def query_objects(language: Any, query: str) -> list[str] | None:
    """The objects a query names, by language; ``None`` when they cannot be
    told (then refused under ``allowed_objects``)."""
    lang = str(getattr(language, "value", language))
    if lang == "sql":
        return sql_objects(query)
    if lang in ("shell", "json"):
        return _mongo_objects(query)
    if lang == "redis":
        return _redis_objects(query)
    if lang == "elasticsearch":
        return _es_objects(query)
    return None


def is_object_allowed(name: str, patterns: Sequence[str]) -> bool:
    """Whether an object name is allowed: a pattern with a dot matches the
    qualified name, one without matches the last part (a table in any schema).
    Redis key patterns and Elasticsearch index patterns in the query are
    matched as written (``rates:*`` is allowed by ``rates:*``, ``*`` is not)."""
    last = name.split(".")[-1]
    for p in patterns:
        regex = glob_to_regex(p)
        if regex.match(name if "." in p else last) or regex.match(name):
            return True
    return False


def objects_error(options: Any, query: str) -> str | None:
    """Why the query is refused under ``allowed_objects``, or ``None``."""
    patterns = _patterns(options, "allowed_objects")
    if not patterns:
        return None
    objects = query_objects(_language(options), query)
    if objects is None:
        return "This source only accepts queries that name the objects they read"
    for obj in objects:
        if not is_object_allowed(obj, patterns):
            return f"'{obj}' is not available on this source"
    return None


# ── Results ──────────────────────────────────────────────────────────


def _mask_value(value: Any, mask: Sequence[str], drop: Sequence[str]) -> Any:
    if isinstance(value, list):
        return [_mask_value(v, mask, drop) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, v in value.items():
        k = str(key)
        if matches_any(k, drop):
            continue
        if matches_any(k, mask):
            out[key] = v if v is None else MASK
        else:
            out[key] = _mask_value(v, mask, drop)
    return out


def apply_column_policy(result: Any, options: Any) -> Any:
    """Masks and drops columns (``mask_columns``, ``drop_columns``) in a
    result, in place; returns it."""
    mask = _patterns(options, "mask_columns")
    drop = _patterns(options, "drop_columns")
    if not mask and not drop:
        return result
    columns = result.columns
    if columns:
        keep = [not matches_any(str(c), drop) for c in columns]
        masked = [matches_any(str(c), mask) for c in columns]
        result.columns = [c for c, k in zip(columns, keep) if k]
        rows = []
        for row in result.rows:
            if isinstance(row, (list, tuple)):
                rows.append([
                    (MASK if masked[i] and v is not None else _mask_value(v, mask, drop))
                    for i, v in enumerate(row)
                    if i >= len(keep) or keep[i]
                ])
            else:
                rows.append(_mask_value(row, mask, drop))
        result.rows = rows
    else:
        result.rows = [_mask_value(row, mask, drop) for row in result.rows]
    return result


def filter_schema(schema: Any, options: Any) -> Any:
    """The schema as the source shows it: allowed tables only, without dropped columns."""
    allowed = _patterns(options, "allowed_objects")
    drop = _patterns(options, "drop_columns")
    if not allowed and not drop:
        return schema
    tables = []
    for table in schema.tables:
        if allowed and not is_object_allowed(table.name, allowed):
            continue
        if drop:
            table = replace(table, columns=[c for c in table.columns if not matches_any(c.name, drop)])
        tables.append(table)
    return replace(schema, tables=tables)


_SHOW_COLLECTIONS = re.compile(r"^show\s+collections", re.IGNORECASE)


def filter_listing(result: Any, options: Any, query: str) -> Any:
    """``show collections`` (MongoDB) lists only the allowed collections."""
    allowed = _patterns(options, "allowed_objects")
    if not allowed or _language(options) not in ("shell", "json") or not _SHOW_COLLECTIONS.match(query.strip()):
        return result
    result.rows = [
        r for r in result.rows
        if isinstance(r, dict) and isinstance(r.get("name"), str) and is_object_allowed(r["name"], allowed)
    ]
    result.row_count = len(result.rows)
    return result


# ── Rate limit ───────────────────────────────────────────────────────


class DataExplorerRateLimiter:
    """Sliding-window counter of executes per source and caller."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._hits: dict[tuple[str, str], list[float]] = {}

    def try_acquire(self, options: Any, caller: Any) -> bool:
        """Records an execute; ``False`` when the caller is over the source's
        limit (not recorded then)."""
        limit = rate_limit_of(options)
        if limit is None:
            return True
        max_queries, window = limit
        who = (getattr(caller, "user_id", None) or getattr(caller, "remote_address", None) or "anonymous"
               if caller is not None else "anonymous")
        key = (str(getattr(options, "name", "")), str(who))
        now = self._now()
        since = now - window
        recent = [t for t in self._hits.get(key, []) if t > since]
        if len(recent) >= max_queries:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True


__all__ = [
    "MASK",
    "DataExplorerRateLimiter",
    "SqlToken",
    "apply_column_policy",
    "can_export",
    "can_use_source",
    "filter_listing",
    "filter_schema",
    "glob_to_regex",
    "is_object_allowed",
    "is_read_only",
    "matches_any",
    "missing_permissions",
    "objects_error",
    "query_objects",
    "rate_limit_of",
    "read_only_error",
    "sql_objects",
    "tokenize_sql",
]
