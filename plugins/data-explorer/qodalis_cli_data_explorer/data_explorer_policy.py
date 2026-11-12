"""The source options that keep data from leaking.

Spec: web-cli ``docs/superpowers/specs/2026-11-12-data-explorer-hardening-design.md``.

Options are read with ``getattr`` and defaults, so this package works with
older versions of the abstractions package (whose options have no such fields).
"""

from __future__ import annotations

import json
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


#: ``$$`` or ``$tag$`` (a tag is an identifier without ``$``: ``$a1$``, ``$é$``, not ``$1$``).
_DOLLAR_TAG = re.compile("\\$(?:[A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff]*)?\\$")
_MYSQL_EXECUTABLE_COMMENT = re.compile(r"/\*M?!\d*")


@dataclass(frozen=True)
class SqlReading:
    """How a dialect reads quotes and comments.  The data explorer does not
    know which database a ``sql`` source is, so a query is checked under the
    reading of each dialect (see :data:`SQL_READINGS`): a quote or comment
    one dialect skips cannot hide a statement another one runs."""

    #: ``\'`` escapes a quote in strings (MySQL; Postgres without
    #: ``standard_conforming_strings``).
    backslash_escapes: bool = False
    #: ``"..."`` is a string, not an identifier (MySQL without ``ANSI_QUOTES``).
    double_quoted_strings: bool = False
    #: ``E'...'`` strings take backslash escapes (Postgres).
    e_strings: bool = False
    #: ``$tag$ ... $tag$`` strings (Postgres).
    dollar_quotes: bool = False
    #: ``/* /* */ */`` comments nest (Postgres, SQL Server).
    nested_comments: bool = False
    #: ``#`` starts a comment, ``--`` only before a space, and the text of
    #: ``/*! ... */`` runs (MySQL).
    mysql_comments: bool = False
    #: ``[name]`` is a quoted identifier (SQL Server, SQLite).
    brackets: bool = False
    #: A character from U+0080 up starts a name only when it is a letter (SQL Server).
    unicode_letters_only: bool = False


#: MySQL (with and without ``ANSI_QUOTES`` and ``NO_BACKSLASH_ESCAPES``),
#: Postgres (with and without ``standard_conforming_strings``), SQL Server
#: and SQLite.
SQL_READINGS = (
    *(
        SqlReading(backslash_escapes=backslash, double_quoted_strings=not ansi, mysql_comments=True)
        for backslash in (True, False)
        for ansi in (False, True)
    ),
    *(
        SqlReading(backslash_escapes=backslash, e_strings=True, dollar_quotes=True, nested_comments=True)
        for backslash in (False, True)
    ),
    SqlReading(nested_comments=True, brackets=True, unicode_letters_only=True),
    SqlReading(brackets=True),
)


def _skip_string(sql: str, i: int, quote: str, backslash_escapes: bool) -> int:
    """The index after the string whose opening quote is at ``i`` (a doubled
    quote is an escape); the end of the text when it is not closed."""
    i += 1
    n = len(sql)
    while i < n:
        if sql[i] == quote and i + 1 < n and sql[i + 1] == quote:
            i += 2
        elif backslash_escapes and sql[i] == "\\":
            i += 2
        elif sql[i] == quote:
            return i + 1
        else:
            i += 1
    return n


def _skip_block_comment(sql: str, i: int, nested: bool) -> int:
    """The index after the ``/* ... */`` comment starting at ``i``."""
    depth = 0
    n = len(sql)
    while i < n:
        if sql.startswith("/*", i) and (nested or depth == 0):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def _quoted_identifier(sql: str, i: int, close: str) -> tuple[str, int]:
    """The name quoted at ``i`` (``"a""b"``, ``[a]]b]``: a doubled closing
    character is an escape) and the index after it."""
    parts: list[str] = []
    j = i + 1
    n = len(sql)
    while j < n:
        end = sql.find(close, j)
        if end < 0:
            parts.append(sql[j:])
            return "".join(parts), n
        parts.append(sql[j:end])
        if end + 1 < n and sql[end + 1] == close:
            parts.append(close)
            j = end + 2
            continue
        return "".join(parts), end + 1
    return "".join(parts), n


def tokenize_sql(sql: str, reading: SqlReading = SQL_READINGS[0]) -> list[SqlToken]:
    """Splits SQL into words, quoted identifiers and punctuation; comments and
    string literals are dropped, so keywords inside them do not count."""
    tokens: list[SqlToken] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-" and (
            not reading.mysql_comments or i + 2 >= n or sql[i + 2].isspace() or ord(sql[i + 2]) < 32
        ):
            # line comment (MySQL needs a space after ``--``: ``1--1`` is ``1 - -1``)
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c == "#" and reading.mysql_comments:
            # MySQL line comment (``#t`` is a SQL Server temp table)
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            m = _MYSQL_EXECUTABLE_COMMENT.match(sql, i) if reading.mysql_comments else None
            if m:
                # MySQL runs the text of /*! ... */ (its */ is read as punctuation)
                i = m.end()
            else:
                i = _skip_block_comment(sql, i, reading.nested_comments)
            continue
        if c == "'" or (c == '"' and reading.double_quoted_strings):
            i = _skip_string(sql, i, c, reading.backslash_escapes)
            continue
        if c == "$" and reading.dollar_quotes:
            # Postgres dollar-quoted string: $tag$ ... $tag$
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end < 0 else end + len(tag)
                continue
        if c in ('"', "`") or (c == "[" and reading.brackets):
            name, i = _quoted_identifier(sql, i, "]" if c == "[" else c)
            tokens.append(SqlToken("ident", name, name))
            continue
        if (c.isascii() and (c.isalpha() or c == "_")) or (
            c >= "\u0080" and (not reading.unicode_letters_only or c.isalpha())
        ):
            # Names may hold non-ASCII letters (``café``): one name, not punctuation before a quote
            j = i + 1
            while j < n and (
                (sql[j].isascii() and (sql[j].isalnum() or sql[j] in "_$"))
                or (sql[j] >= "\u0080" and (sql[j].isalnum() or sql[j].isnumeric()))
            ):
                j += 1
            raw = sql[i:j]
            if reading.e_strings and raw in ("E", "e") and j < n and sql[j] == "'":
                # Postgres escape string: E'it\'s'
                i = _skip_string(sql, j, "'", True)
                continue
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
    "REVOKE", "DENY", "COPY", "CALL", "EXEC", "EXECUTE", "INTO", "LOCK", "VACUUM", "ATTACH", "DETACH", "PRAGMA", "SET",
    # SQL Server runs statements without ``;`` between them: ``SELECT 1 COMMIT …`` would keep what follows
    "COMMIT", "ROLLBACK",
}
#: Write keywords that are also read-only functions when a ``(`` follows
#: (``REPLACE(name, 'a', 'b')``).
_SQL_FUNCTION_WORDS = {"REPLACE", "INSERT"}


#: A carriage return not followed by a newline: Postgres ends a ``--``
#: comment there, the other databases do not.
_LONE_CARRIAGE_RETURN = re.compile(r"\r(?!\n)")


def _sql_read_only_error(query: str) -> str | None:
    if _LONE_CARRIAGE_RETURN.search(query):
        return "Read-only source: end lines with \\n or \\r\\n"
    for reading in SQL_READINGS:
        error = _sql_read_only_error_as(query, reading)
        if error:
            return error
    return None


def _sql_read_only_error_as(query: str, reading: SqlReading) -> str | None:
    statements: list[list[SqlToken]] = [[]]
    for t in tokenize_sql(query, reading):
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
#: Words that end a FROM or USING list at its own depth.
_SQL_LIST_END_WORDS = {
    "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "UNION", "EXCEPT", "INTERSECT", "WINDOW", "FETCH", "FOR",
    "RETURNING", "SELECT", "FROM", "QUALIFY", "OPTION", "INTO", "SET", "VALUES",
}
#: Words after a leading EXPLAIN, DESCRIBE or DESC that do not name a table (``EXPLAIN ANALYZE SELECT …``).
_SQL_EXPLAIN_WORDS = {
    "SELECT", "WITH", "ANALYZE", "VERBOSE", "FORMAT", "EXTENDED", "PARTITIONS", "QUERY", "TABLE", "VALUES", "FOR",
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE",
}


def sql_objects(query: str) -> list[str]:
    """The tables a SQL query names (after FROM, USING, JOIN, INTO, UPDATE,
    TABLE and a leading DESCRIBE or EXPLAIN), without the CTE names it
    defines, under every reading of its strings."""
    objects: list[str] = []
    for reading in SQL_READINGS:
        for name in _sql_objects_as(query, reading):
            if name not in objects:
                objects.append(name)
    return objects


def _sql_objects_as(query: str, reading: SqlReading) -> list[str]:
    return _walk_sql_objects(tokenize_sql(query, reading))[0]


def _walk_sql_objects(tokens: list[SqlToken]) -> tuple[list[str], set[str], set[int]]:
    """The tables the tokens name (without CTE names); the lower-case names
    a row of them goes by (tables, CTEs, aliases); and the indices of the
    tokens that name or alias them."""

    def at(k: int) -> SqlToken | None:
        return tokens[k] if 0 <= k < len(tokens) else None

    def is_name(t: SqlToken | None) -> bool:
        return t is not None and t.kind in ("ident", "word")

    def is_punct(t: SqlToken | None, v: str) -> bool:
        return t is not None and t.kind == "punct" and t.value == v

    def value(k: int) -> str | None:
        t = at(k)
        return t.value if t is not None else None

    # Parenthesis depth of each token (a parenthesis has the depth outside it)
    depth_at: list[int] = []
    depth = 0
    for t in tokens:
        if is_punct(t, ")"):
            depth -= 1
        depth_at.append(depth)
        if is_punct(t, "("):
            depth += 1

    # CTE names: name [ (columns) ] AS ( — they stand for a table only until
    # the end of the parentheses (or the statement) they are defined in
    ctes: list[tuple[str, int, int]] = []
    defined: set[int] = set()
    for i, tok in enumerate(tokens):
        if not is_name(tok):
            continue
        j = i + 1
        if is_punct(at(j), "("):
            level = 0
            while j < len(tokens):
                if is_punct(tokens[j], "("):
                    level += 1
                elif is_punct(tokens[j], ")"):
                    level -= 1
                    if level == 0:
                        break
                j += 1
            j += 1
        if value(j) == "AS" and is_punct(at(j + 1), "("):
            end = i + 1
            while end < len(tokens) and not is_punct(tokens[end], ";") and not (
                is_punct(tokens[end], ")") and depth_at[end] < depth_at[i]
            ):
                end += 1
            ctes.append((tok.raw.lower(), i, end))
            defined.add(i)

    def is_cte(name: str, pos: int) -> bool:
        return any(c[0] == name and c[1] < pos < c[2] for c in ctes)

    objects: list[str] = []
    row_names: set[str] = {c[0] for c in ctes}
    openers: list[str | None] = []

    def read_name(start: int) -> tuple[str, int] | None:
        """A dotted name; ``..`` is an empty part (``db..table``, SQL Server's default schema)."""
        if not is_name(at(start)):
            return None
        parts = [tokens[start].raw]
        k = start + 1
        while is_punct(at(k), "."):
            if is_name(at(k + 1)):
                parts.append(tokens[k + 1].raw)
                k += 2
            elif is_punct(at(k + 1), "."):
                parts.append("")
                k += 1
            else:
                break
        return ".".join(parts), k

    def read_table(k: int, in_list: bool) -> int:
        """A table (with its alias) at ``k``; in a FROM or USING list ``f(…)``
        is a function, not a table.  Returns the index after it."""
        if value(k) in ("ONLY", "LATERAL") and at(k).kind == "word":  # type: ignore[union-attr]
            k += 1
        name = read_name(k)
        if name is None or (in_list and is_punct(at(name[1]), "(")):
            return k
        if not is_cte(name[0].lower(), k):
            objects.append(name[0])
        row_names.add(name[0].split(".")[-1].lower())
        defined.update(range(k, name[1]))
        k = name[1]
        # An alias: [AS] alias
        nxt = at(k)
        if value(k) == "AS":
            if is_name(at(k + 1)):
                row_names.add(tokens[k + 1].raw.lower())
                defined.add(k + 1)
            k += 2
        elif (
            nxt is not None and nxt.kind == "word"
            and nxt.value not in _SQL_CLAUSE_WORDS and nxt.value not in _SQL_LIST_END_WORDS
        ):
            row_names.add(nxt.raw.lower())
            defined.add(k)
            k += 1
        return k

    for i, t in enumerate(tokens):
        if is_punct(t, "("):
            prev = at(i - 1)
            openers.append(prev.value if prev is not None and prev.kind == "word" else None)
            continue
        if is_punct(t, ")"):
            if openers:
                openers.pop()
            continue
        if t.kind != "word":
            continue
        statement_start = i == 0 or is_punct(at(i - 1), ";")
        if statement_start and t.value in ("DESCRIBE", "DESC", "EXPLAIN"):
            nxt = at(i + 1)
            if is_name(nxt) and not (nxt.kind == "word" and nxt.value in _SQL_EXPLAIN_WORDS):  # type: ignore[union-attr]
                read_table(i + 1, False)
            continue
        if t.value in ("FROM", "USING"):
            if t.value == "FROM" and openers and openers[-1] in _SQL_FROM_FUNCTIONS:
                continue
            # Every item of the list: a table, a subquery or a function, up to the next ``,`` at this depth
            k = i + 1
            while True:
                k = read_table(k, True)
                level = 0
                following = -1
                while k < len(tokens):
                    u = tokens[k]
                    if is_punct(u, "("):
                        level += 1
                    elif is_punct(u, ")"):
                        if level == 0:
                            break
                        level -= 1
                    elif is_punct(u, ";"):
                        break
                    elif level == 0 and is_punct(u, ","):
                        following = k + 1
                        break
                    elif level == 0 and u.kind == "word" and u.value in _SQL_LIST_END_WORDS:
                        break
                    k += 1
                if following < 0:
                    break
                k = following
            continue
        if t.value in _SQL_OBJECT_KEYWORDS:
            read_table(i + 1, False)
    return objects, row_names, defined


# ── MongoDB ──────────────────────────────────────────────────────────

_MONGO_READ_METHODS = {"find", "findone", "countdocuments", "estimateddocumentcount", "distinct", "aggregate"}
_MONGO_SHOW = re.compile(r"^show\s+(collections|dbs|databases)\s*;?$", re.IGNORECASE)
_MONGO_CALL = re.compile(
    r"""^db\.(?:getCollection\(\s*['"]([^'"]+)['"]\s*\)|([A-Za-z0-9_$-]+(?:\.[A-Za-z0-9_$-]+)*?))\.(\w+)\s*\("""
)
_MONGO_WRITE_STAGE = re.compile(r"""["']?\$(out|merge)["']?\s*:""")
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
#: Pipeline stages that read or write other collections.
_MONGO_COLLECTION_STAGES = re.compile(r"\$(lookup|graphLookup|unionWith|out|merge)")
#: A key naming a collection in those stages: a string value is one, ``{``
#: nests, anything else cannot be told.
_MONGO_STAGE_COLLECTION = re.compile(
    r"""["']?(?:\b(?:from|coll|into)|\$(?:unionWith|out|merge))["']?\s*:\s*(?:"([^"]*)"|'([^']*)'|(\{))?"""
)


def _decode_json_escapes(text: str) -> str:
    """``\\uXXXX`` escapes decoded, as the driver reads them (``"\\u0024out"`` is ``$out``)."""
    return _JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _mongo_read_only_error(query: str) -> str | None:
    q = _decode_json_escapes(query.strip())
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
    q = _decode_json_escapes(query.strip())
    if _MONGO_SHOW.match(q):
        return []
    m = _MONGO_CALL.match(q)
    if m is None:
        return None
    objects = [m.group(1) or m.group(2)]
    if _MONGO_COLLECTION_STAGES.search(q):
        for stage in _MONGO_STAGE_COLLECTION.finditer(q):
            if stage.group(1) is not None or stage.group(2) is not None:
                objects.append(stage.group(1) if stage.group(1) is not None else stage.group(2))
            elif stage.group(3) is None:
                return None
    return objects


# ── Redis ────────────────────────────────────────────────────────────

_REDIS_READ = {
    "GET", "MGET", "STRLEN", "GETRANGE", "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS", "LRANGE",
    "LLEN", "LINDEX", "SMEMBERS", "SCARD", "SISMEMBER", "ZRANGE", "ZRANGEBYSCORE", "ZREVRANGE", "ZCARD", "ZSCORE",
    "TTL", "PTTL", "EXISTS", "TYPE", "KEYS", "SCAN", "INFO", "DBSIZE", "PING",
}
_REDIS_NO_KEYS = {"INFO", "DBSIZE", "PING"}
#: Commands whose every argument is a key.
_REDIS_ALL_KEYS = {"MGET", "EXISTS", "DEL", "UNLINK", "TOUCH"}


def _redis_words(query: str) -> list[str]:
    """The words of a Redis command, split as the provider splits them:
    quotes group and join the text around them, no escapes."""
    words: list[str] = []
    word = ""
    quote: str | None = None
    for c in query.strip():
        if quote:
            if c == quote:
                quote = None
            else:
                word += c
        elif c in ('"', "'"):
            quote = c
        elif c.isspace():
            if word:
                words.append(word)
            word = ""
        else:
            word += c
    if word:
        words.append(word)
    return words


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

#: POST endpoints that only read, after the index names.
_ES_READ_POST = re.compile(r"^(.*)/(_search|_count|_msearch|_mget|_field_caps|_validate/query)$")
_ES_BODY_INDEX = re.compile(r'"_?index"\s*:')
_ES_REQUEST_LINE = re.compile(r"^(GET|POST|PUT|DELETE|HEAD|PATCH)\s+(\S+)", re.IGNORECASE)


def _es_request_line(query: str) -> tuple[str, str]:
    """The verb and the bare path (cut at ``?`` or ``#``: the HTTP client drops the fragment)."""
    first = query.strip().split("\n")[0].strip()
    m = _ES_REQUEST_LINE.match(first)
    verb = m.group(1).upper() if m else "GET"
    raw = m.group(2) if m else first
    path = raw if raw.startswith("/") else f"/{raw}"
    return verb, re.split(r"[?#]", path)[0]


def _es_read_only_error(query: str) -> str | None:
    verb, path = _es_request_line(query)
    if verb in ("GET", "HEAD"):
        return None
    m = _ES_READ_POST.match(path)
    # Only index names before the endpoint: ``/logs/_doc/_search`` indexes a document
    if verb == "POST" and m and all(not s.startswith("_") or s == "_all" for s in m.group(1).split("/") if s):
        return None
    return f"Read-only source: {verb} {path} is not allowed"


def _es_objects(query: str) -> list[str] | None:
    _, path = _es_request_line(query)
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0].startswith("_"):
        return None
    # ``_msearch`` headers, ``_mget`` docs and terms lookups name indices in the body
    body = _decode_json_escapes("\n".join(query.strip().split("\n")[1:]))
    if _ES_BODY_INDEX.search(body):
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
        # A lone carriage return: the objects cannot be told (see _sql_read_only_error)
        return None if _LONE_CARRIAGE_RETURN.search(query) else sql_objects(query)
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


# ── Masked columns ───────────────────────────────────────────────────

#: Words never taken for column names when checking masked columns.
_SQL_KEYWORDS = (
    _SQL_READ_STARTS | _SQL_WRITE_WORDS | _SQL_OBJECT_KEYWORDS | _SQL_CLAUSE_WORDS | {
        "AS", "DISTINCT", "ALL", "TOP", "BY", "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE", "ILIKE", "BETWEEN",
        "CASE", "WHEN", "THEN", "ELSE", "END", "ASC", "EXISTS", "ANY", "SOME", "TRUE", "FALSE", "ONLY", "LATERAL",
    }
)
#: What may come right before a masked column in the select list.
_SELECT_ITEM_BEFORE = {"SELECT", "DISTINCT", "ALL", "TOP", ","}
_SET_OPERATIONS = {"UNION", "EXCEPT", "INTERSECT", "MINUS"}


def masked_column_message(name: str) -> str:
    return f"Masked column '{name}' can only be selected by its own name"


def whole_row_message(name: str) -> str:
    return f"'{name}' is a whole row and this source masks columns: select its columns by name"


#: Postgres identifiers with Unicode escapes (``U&"\\0065mail"``, ``UESCAPE``).
_UNICODE_IDENTIFIER = re.compile(r'[Uu]&"')
UNICODE_IDENTIFIER_MESSAGE = 'This source masks columns: write names without Unicode escapes (U&"...")'


def _masked_scan(options: Any, query: str) -> tuple[list[tuple[str, bool]], str | None]:
    """The masked or dropped columns a SQL query names, each with whether
    every mention, under every reading, is a plain item of the outer select
    list (``SELECT email``, ``SELECT u.email,``): outside parentheses, before
    the first ``FROM``, followed by ``,``, ``FROM`` or the end, in a query
    without ``UNION``/``EXCEPT``/``INTERSECT``/``MINUS``.  The second value
    refuses the query outright: a whole row used as a value (Postgres
    ``SELECT u FROM users u``, ``row_to_json(u)``) or a Unicode-escaped name."""
    patterns = _patterns(options, "mask_columns") + _patterns(options, "drop_columns")
    if not patterns or _language(options) != "sql":
        return [], None
    if _UNICODE_IDENTIFIER.search(query):
        return [], UNICODE_IDENTIFIER_MESSAGE
    #: lower-case name → (as first written, every mention plain)
    found: dict[str, tuple[str, bool]] = {}
    for reading in SQL_READINGS:
        tokens = tokenize_sql(query, reading)
        _, row_names, defined = _walk_sql_objects(tokens)
        set_operation = any(t.kind == "word" and t.value in _SET_OPERATIONS for t in tokens)
        depth = 0
        in_select_list = False
        for i, t in enumerate(tokens):
            if t.kind == "punct" and t.value == "(":
                depth += 1
                continue
            if t.kind == "punct" and t.value == ")":
                depth -= 1
                continue
            if t.kind == "punct" and t.value == ";":
                in_select_list = False
                continue
            if depth == 0 and t.kind == "word" and t.value in ("SELECT", "FROM"):
                in_select_list = t.value == "SELECT"
            if i in defined or t.kind not in ("word", "ident") or (t.kind == "word" and t.value in _SQL_KEYWORDS):
                continue
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            # A qualifier (``u.``) or a function (``upper(``)
            if nxt is not None and nxt.kind == "punct" and nxt.value in (".", "("):
                continue
            prev = tokens[i - 1] if i > 0 else None
            qualified = prev is not None and prev.kind == "punct" and prev.value == "."
            if not qualified and t.raw.lower() in row_names:
                return [], whole_row_message(t.raw)
            if not matches_any(t.raw, patterns):
                continue
            k = i - 2 if qualified and i >= 2 and tokens[i - 2].kind in ("word", "ident") else i
            before = tokens[k - 1].value if k >= 1 else None
            plain = (
                depth == 0 and in_select_list and not set_operation
                and before in _SELECT_ITEM_BEFORE
                and (nxt is None or nxt.value in (",", "FROM", ";"))
            )
            name, all_plain = found.get(t.raw.lower(), (t.raw, True))
            found[t.raw.lower()] = (name, all_plain and plain)
    return list(found.values()), None


def masked_column_error(options: Any, query: str) -> str | None:
    """Why a SQL query is refused under ``mask_columns``/``drop_columns``
    (before it runs): it names a masked or dropped column other than as a
    plain item of the select list (``SELECT email AS e``, ``upper(email)``,
    ``WHERE email = ...``), uses a whole row as a value, or escapes a name;
    ``None`` otherwise."""
    references, refused = _masked_scan(options, query)
    if refused:
        return refused
    for name, plain in references:
        if not plain:
            return masked_column_message(name)
    return None


def masked_result_error(options: Any, query: str, columns: Sequence[str] | None) -> str | None:
    """Why a result is withheld: a masked or dropped column the query selects
    is missing from the result's columns (it came back under another name,
    e.g. SQL Server ``FOR JSON``), or ``None``.  ``columns`` are the
    result's columns before masking."""
    names = {str(c).lower() for c in columns or []}
    for name, _ in _masked_scan(options, query)[0]:
        if name.lower() not in names:
            return masked_column_message(name)
    return None

# ── Results ──────────────────────────────────────────────────────────


def _mask_value(value: Any, mask: Sequence[str], drop: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _mask_json_text(value, mask, drop)
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


def _mask_json_text(text: str, mask: Sequence[str], drop: Sequence[str]) -> str:
    """A string holding a JSON object or array (a JSON column, an
    Elasticsearch answer) masked like a document and written back compact;
    kept as written when nothing changed or it is not JSON."""
    start = text.lstrip()[:1]
    if start not in ("{", "["):
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    masked = _mask_value(parsed, mask, drop)
    if masked == parsed:
        return text
    return json.dumps(masked, separators=(",", ":"), ensure_ascii=False)


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
    "masked_column_error",
    "masked_column_message",
    "masked_result_error",
    "whole_row_message",
    "matches_any",
    "missing_permissions",
    "objects_error",
    "query_objects",
    "rate_limit_of",
    "read_only_error",
    "sql_objects",
    "tokenize_sql",
]
