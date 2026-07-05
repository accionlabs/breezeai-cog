"""Shared, cross-language detectors (the value that lived in the old ``utils.js`` /
``routes-js-core.js``). Per-language parsers extract a normalized call and these
classify it — so API/DB detection isn't reimplemented per language.
"""

from __future__ import annotations

from .api_calls import match_api
from .db_queries import match_db
from .queries import is_query, text_has_query


def classify_call(
    callee: str, method: str, arg: str | None = None, language: str | None = None
) -> tuple[str, str, str | None] | None:
    """Classify a normalized call into ``(semanticType, method, dataAccessHint)``.

    * HTTP client call    -> ``("api_call", "<VERB>", None)``
    * raw SQL/JPQL query  -> ``("query_statement", "<method>", None)``
    * ORM/DB method call  -> ``("db_method_call", "<method>", "<hint>")``
    * otherwise           -> ``None``

    ``arg`` = the call's first string literal (for SQL sniffing). ``language`` = the source
    language tag, passed to :func:`match_db` for its .NET/EF gate (``None`` = unknown, stays
    permissive). API is tried first (so ``objects.get`` is a Django query, not an HTTP GET);
    raw queries before ORM (so ``createNativeQuery("SELECT …")`` is a ``query_statement``).
    """
    verb = match_api(callee, method)
    if verb is not None:
        return "api_call", verb, None
    if is_query(method, arg):
        return "query_statement", method, None
    hint = match_db(callee, method, language)
    if hint is not None:
        return "db_method_call", method, hint
    return None


__all__ = ["classify_call", "match_api", "match_db", "is_query", "text_has_query"]
