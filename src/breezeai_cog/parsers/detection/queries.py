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
