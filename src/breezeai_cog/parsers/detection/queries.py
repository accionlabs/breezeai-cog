"""Raw-query detection → ``query_statement`` (Data-access view).

Fires when a statement runs a **raw** SQL/JPQL query — either a string literal with real
SQL *structure* (not just a leading keyword, so UI strings like "Create account" don't
match) or a call to a strong raw-query builder (``$queryRaw``, ``createNativeQuery``,
``executeQuery``, JDBC ``prepareStatement``, …). This is distinct from ORM method calls
(``db_method_call``), which stay as-is.
"""

from __future__ import annotations

import re

from .api_calls import _ends_with_word

# Require structure, not just a leading verb, to avoid natural-language false positives.
_SQL_RE = re.compile(
    r"^\s*\(?\s*("
    r"SELECT\b[\s\S]+\bFROM\b"
    r"|INSERT\s+INTO\b"
    r"|UPDATE\b[\s\S]+\bSET\b"
    r"|DELETE\s+FROM\b"
    # allow index-type qualifiers (SQL UNIQUE / Neo4j VECTOR|TEXT|POINT|RANGE|FULLTEXT|LOOKUP)
    # between CREATE and the object keyword, e.g. `CREATE VECTOR INDEX`, `CREATE UNIQUE INDEX`.
    r"|CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:UNIQUE|VECTOR|TEXT|POINT|RANGE|FULLTEXT|LOOKUP|BTREE|HASH|CLUSTERED|NONCLUSTERED)\s+)*(?:TABLE|INDEX|VIEW|DATABASE|SCHEMA|SEQUENCE|CONSTRAINT)\b"
    r"|DROP\s+(?:TABLE|INDEX|VIEW|DATABASE|SEQUENCE|CONSTRAINT)\b"
    r"|ALTER\s+TABLE\b"
    r"|WITH\b[\s\S]+\bAS\b[\s\S]+\bSELECT\b"
    r"|MERGE\s+INTO\b"
    # `TRUNCATE TABLE x`, or `TRUNCATE x` ending there — not a sentence ("Truncate nothing in AOF")
    r"|TRUNCATE\s+TABLE\b"
    r"|TRUNCATE\s+(?:ONLY\s+)?[\w.\"`\[\]]+\s*(?:[;,)]|CASCADE\b|RESTART\b|CONTINUE\b|$)"
    r")",
    re.IGNORECASE,
)

# Builders that execute a raw query even when the SQL isn't a captured literal
# (e.g. `em.createNativeQuery(sql)`, `prisma.$queryRaw`...).
_STRONG_QUERY_METHODS = {
    "$queryraw", "$queryrawunsafe", "$executeraw", "$executerawunsafe",
    "createnativequery", "executequery", "executeupdate",
    "preparestatement", "nativequery", "rawquery",
}

# ``createQuery`` is JPA's ``em.createQuery(jpql)`` and Hibernate's ``session.createQuery(hql)``,
# but the name is common elsewhere: a model's ``Query.createQuery(key, value)`` factory, LINQ's
# ``IQueryProvider.CreateQuery(expression)``. Without a query string as its argument it is a raw
# query only in a file whose imports name JPA or Hibernate (``vendor_from_imports``).
_ORM_QUERY_METHODS = {"createquery"}
_ORM_QUERY_VENDORS = frozenset({"jpa", "hibernate"})


def is_query(method: str, arg: str | None, vendor: str | None = None) -> bool:
    """Whether a call runs a raw query: its first string argument is SQL/JPQL, or the method is
    a raw-query builder. ``vendor`` is the ORM the file's imports name."""
    if arg and _SQL_RE.match(arg):
        return True
    m = method.lower()
    if m in _ORM_QUERY_METHODS:
        return vendor in _ORM_QUERY_VENDORS
    return m in _STRONG_QUERY_METHODS


# The SQL forms recognised in a statement's string literals. Structure, not a leading verb.
_SQL_FORMS = (
    r"""SELECT\b[\s\S]+?\bFROM\b"""
    r"""|INSERT\s+INTO\b"""
    r"""|UPDATE\b[\s\S]+?\bSET\b"""
    r"""|DELETE\s+FROM\b"""
    r"""|CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:UNIQUE|VECTOR|TEXT|POINT|RANGE|FULLTEXT|LOOKUP|BTREE|HASH|CLUSTERED|NONCLUSTERED)\s+)*(?:TABLE|INDEX|VIEW|SEQUENCE|CONSTRAINT)\b"""
    r"""|ALTER\s+TABLE\b"""
    r"""|MERGE\s+INTO\b"""
)

# A quoted (string-literal) SQL query embedded anywhere in a statement's source —
# catches `String sql = "SELECT … FROM …"`, `@Query("…")`, `` prisma.$queryRaw`…` ``.
# The leading quote/backtick keeps SQL-in-comments and identifiers from matching. A match may
# run on into a later, unrelated literal, so a statement's own check confirms it on its string
# literals (``string_has_query``).
_SQL_IN_TEXT = re.compile(r"""["'`]\s*(?:""" + _SQL_FORMS + ")", re.IGNORECASE)
# The same forms at the start of a string's content (its quotes already removed).
_SQL_AT_START = re.compile(r"\s*(?:" + _SQL_FORMS + ")", re.IGNORECASE)
_SQL_VERB = re.compile(r"\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|MERGE)\b", re.IGNORECASE)


def built_string_has_query(parts: "list[str]") -> bool:
    """True if a string built from ``parts`` (a local's literals, joined as
    ``string_has_query`` joins them) starts with SQL — the check a call's literal argument gets
    (``is_query``), so ``WITH … AS (…) SELECT`` counts. Built SQL starts with its verb in the
    first piece, so a verb further in (HTML or prose appended later) never counts."""
    return bool(_SQL_RE.match(" ".join(parts)))


# Calls that run the SQL string they are given, on any receiver: DB-API and ADO.NET
# ``execute*``, ``prepare*``, pandas ``read_sql``, EF ``FromSqlRaw``, JPA/Hibernate native
# queries, ``$wpdb`` ``get_results``/``get_var``/``get_row``/``get_col``, JDBC ``addBatch``.
_SQL_SINK_PREFIXES = ("execute",)
_SQL_SINKS = frozenset({
    "prepare", "preparestatement", "preparecall", "read_sql", "read_sql_query", "rawquery",
    "fromsqlraw", "fromsql", "sqlquery", "createnativequery", "createsqlquery", "nativequery",
    "get_results", "get_var", "get_row", "get_col", "addbatch",
})
# Names that run SQL only on a database handle (PHP/node ``db.query``, Dapper ``conn.Query*``,
# node-sqlite ``db.run``/``db.all``, Laravel ``DB::select``/``statement``, Django ``raw``):
# elsewhere they are an LLM chain's ``run``, an index's ``query``, a process's ``exec``.
_HANDLE_SINK_PREFIXES = ("query", "exec")
_HANDLE_SINKS = frozenset({"run", "all", "select", "statement", "raw", "unprepared"})
_NEVER_SINK_PREFIXES = ("queryselector",)  # DOM ``querySelector*``
# A receiver that names a database handle: the whole name (``db``, ``cur``, ``pool``, ``DB::``,
# ``$wpdb``), or a name ending in a handle word that starts a word there (``this.db``, ``mainDb``,
# ``dbConn``, ``jdbcTemplate``) — never a name that only contains the letters (``sqlTokenRe``,
# ``webdb``, ``connector``). ``client`` is left out (Apollo ``client.query``, an LLM client), and
# ``pool`` counts only as the whole name (node-pg ``pool.query``, not ``workerPool.exec``), as
# ``db_queries._DB_RECEIVER_SUFFIXES`` decides for ORM calls.
_DB_HANDLE_NAMES = frozenset({
    "db", "con", "cnn", "conn", "connection", "cur", "cursor", "pg", "tx", "trx", "pool",
    "session", "stmt", "statement", "knex", "sequelize", "transaction", "adapter", "sql",
    "pdo", "wpdb", "database", "jdbc",
})
_DB_HANDLE_WORDS = (
    "db", "conn", "connection", "cursor", "database", "datasource", "jdbctemplate",
    "transaction", "pdo",
)


def _is_db_handle(name: str) -> bool:
    return name.lower() in _DB_HANDLE_NAMES or any(_ends_with_word(name, w) for w in _DB_HANDLE_WORDS)
# Database command types whose constructor takes the SQL (``new SqlCommand(sql, conn)``).
_SQL_COMMAND = re.compile(
    r"^(?:\w+\.)*(?:Sql|SqlCe|OleDb|Odbc|Oracle|MySql|Npgsql|Sqlite|SQLite|DB2|Db2|Fb|Ifx|Db)"
    r"(?:Command|DataAdapter)$"
)


# PHP's procedural database functions take the connection first and the SQL after it.
_SQL_ARGUMENT = {
    "mysqli_query": 1, "mysqli_real_query": 1, "mysqli_multi_query": 1, "mysqli_prepare": 1,
    "pg_query": 1, "pg_send_query": 1, "pg_prepare": 2, "sqlsrv_query": 1,
    "sqlsrv_prepare": 1, "oci_parse": 1, "odbc_exec": 1, "odbc_prepare": 1, "db2_exec": 1,
    "db2_prepare": 1, "ibase_query": 1, "mysql_query": 0,
}


def is_sql_sink(method: str, callee: str = "") -> bool:
    """Whether a call runs the SQL string passed to it (see ``sql_argument``). ``callee`` is
    the call's full target (``self.db.query``); a generic name counts only when the receiver
    just before it names a database handle."""
    m = method.lower()
    if m.startswith(_NEVER_SINK_PREFIXES):
        return False
    if m.startswith(_SQL_SINK_PREFIXES) or m in _SQL_SINKS or m in _SQL_ARGUMENT:
        return True
    if not (m.startswith(_HANDLE_SINK_PREFIXES) or m in _HANDLE_SINKS):
        return False
    words = re.findall(r"[A-Za-z_]\w*", callee)
    at = max((i for i, w in enumerate(words) if w.lower() == m), default=-1)
    return at > 0 and _is_db_handle(words[at - 1])


def sql_argument(method: str, count: int) -> int:
    """The position of the SQL among a call's ``count`` arguments: the first, or after the
    connection for PHP's procedural functions (``mysqli_query($conn, $sql)``; ``pg_query($sql)``
    with the connection left out)."""
    at = _SQL_ARGUMENT.get(method.lower(), 0)
    return at if at < count else 0


def is_sql_command_type(name: str) -> bool:
    """Whether ``name`` is a database command type taking the SQL first (``SqlCommand``…)."""
    return bool(_SQL_COMMAND.match(name.strip()))


def text_has_query(text: str) -> bool:
    """True if the statement's source embeds a raw SQL string literal (structure-checked)."""
    return bool(_SQL_IN_TEXT.search(text))


def string_has_query(parts: "list[str | None]") -> bool:
    """True if one string value holds SQL. ``parts`` are the contents (quotes removed) of the
    literals of one string: a single literal, or the literals of one concatenation in order,
    ``None`` standing for a non-literal operand (``"select " + cols + " from t"``). The query
    starts a literal and may continue over the next parts; a quoted query inside a literal
    (T-SQL ``print 'alter table …'``) counts too."""
    joined, starts = "", []
    for part in parts:
        if part is not None:
            starts.append(len(joined))
        joined += part if part is not None else " "
        joined += " "
    return any(_SQL_VERB.match(joined, i) and _SQL_AT_START.match(joined, i) for i in starts) \
        or bool(_SQL_IN_TEXT.search(joined))
