"""Raw-query detection → ``query_statement`` (spec A4 / B4 Data-access view).

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
# (e.g. `em.createNativeQuery(sql)`, `prisma.$queryRaw`...).
_STRONG_QUERY_METHODS = {
    "$queryraw", "$queryrawunsafe", "$executeraw", "$executerawunsafe",
    "createnativequery", "createquery", "executequery", "executeupdate",
    "preparestatement", "nativequery", "rawquery",
}


def is_query(method: str, arg: str | None) -> bool:
    if arg and _SQL_RE.match(arg):
        return True
    return method.lower() in _STRONG_QUERY_METHODS


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
