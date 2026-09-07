"""Raw-query detection → ``query_statement`` (Data-access view).

Fires when a statement runs a **raw** SQL/JPQL query — either a string literal with real
SQL *structure* (not just a leading keyword, so UI strings like "Create account" don't
match) or a call to a strong raw-query builder (``$queryRaw``, ``createNativeQuery``,
``executeQuery``, JDBC ``prepareStatement``, …). This is distinct from ORM method calls
(``db_method_call``), which stay as-is.
"""

from __future__ import annotations

import re

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
    r"|TRUNCATE\s+(TABLE\b)?"
    r")",
    re.IGNORECASE,
)

# Builders that execute a raw query even when the SQL isn't a captured literal
# (e.g. `em.createNativeQuery(sql)`, `prisma.$queryRaw`, `$wpdb->get_results`...).
_STRONG_QUERY_METHODS = {
    "$queryraw", "$queryrawunsafe", "$executeraw", "$executerawunsafe",
    "createnativequery", "createquery", "executequery", "executeupdate",
    "preparestatement", "nativequery", "rawquery",
    "get_results", "get_row", "get_col", "get_var",
}

# Callee substrings that positively signal a database connection or raw driver handle
_QUERY_CALLEE_HINTS = ("pdo", "wpdb", "connection", "conn", "db")


def is_query(method: str, arg: str | None, callee: str | None = None) -> bool:
    if arg and _SQL_RE.match(arg):
        return True
    m = method.lower()
    if m in _STRONG_QUERY_METHODS:
        return True
    if callee is not None:
        low_callee = callee.lower()
        if "pdo" in low_callee and m in ("query", "prepare", "exec", "execute"):
            return True
        if "wpdb" in low_callee and m in ("get_results", "get_row", "get_col", "get_var", "query"):
            return True
    return False


# A quoted (string-literal) SQL query embedded anywhere in a statement's source —
# catches `String sql = "SELECT … FROM …"`, `@Query("…")`, `` prisma.$queryRaw`…` ``.
# The leading quote/backtick keeps SQL-in-comments and identifiers from matching.
_SQL_IN_TEXT = re.compile(
    r"""["'`]\s*(?:"""
    r"""SELECT\b[\s\S]+?\bFROM\b"""
    r"""|INSERT\s+INTO\b"""
    r"""|UPDATE\b[\s\S]+?\bSET\b"""
    r"""|DELETE\s+FROM\b"""
    r"""|CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:UNIQUE|VECTOR|TEXT|POINT|RANGE|FULLTEXT|LOOKUP|BTREE|HASH|CLUSTERED|NONCLUSTERED)\s+)*(?:TABLE|INDEX|VIEW|SEQUENCE|CONSTRAINT)\b"""
    r"""|ALTER\s+TABLE\b"""
    r"""|MERGE\s+INTO\b"""
    r""")""",
    re.IGNORECASE,
)


def text_has_query(text: str) -> bool:
    """True if the statement's source embeds a raw SQL string literal (structure-checked)."""
    return bool(_SQL_IN_TEXT.search(text))
