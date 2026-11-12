"""Shared, cross-language detectors (the value that lived in the old ``utils.js`` /
``routes-js-core.js``). Per-language parsers extract a normalized call and these
classify it — so API/DB detection isn't reimplemented per language.
"""

from __future__ import annotations

from collections.abc import Mapping

from .api_calls import match_api
from .db_queries import match_db, vendor_from_imports
from .queries import is_query, string_has_query, text_has_query


def classify_call(
    callee: str,
    method: str,
    arg: str | None = None,
    language: str | None = None,
    typed_db_ids: "frozenset[str] | None" = None,
    http_client_ids: "frozenset[str] | None" = None,
    vendor: str | None = None,
    typed_receivers: "Mapping[str, str] | None" = None,
) -> tuple[str, str | None, str | None] | None:
    """Classify a normalized call into ``(semanticType, method, dataAccessHint)``.

    * HTTP client call    -> ``("api_call", "<VERB>", None)``; a generic ``request(…)`` whose verb
      the call does not name -> ``("api_call", None, None)`` (``method`` is an HTTP verb or empty)
    * raw SQL/JPQL query  -> ``("query_statement", "<method>", None)``
    * ORM/DB method call  -> ``("db_method_call", "<method>", "<hint>")``
    * otherwise           -> ``None``

    ``arg`` = the call's first string literal (for SQL sniffing). ``language`` = the source
    language tag, passed to :func:`match_db` for its language gates (``None`` = unknown, stays
    permissive); ``vendor`` = the ORM the file's imports name (``vendor_from_imports``);
    ``typed_receivers`` = names declared as a datastore client type → that product.
    API is tried first (so ``objects.get`` is a Django query, not an HTTP GET);
    raw queries before ORM (so ``createNativeQuery("SELECT …")`` is a ``query_statement``).
    """
    verb = match_api(callee, method, http_client_ids=http_client_ids, language=language)
    if verb is not None:
        return "api_call", (None if verb == "REQUEST" else verb), None
    if is_query(method, arg, vendor):
        return "query_statement", method, None
    hint = match_db(callee, method, language, typed_db_ids=typed_db_ids, vendor=vendor,
                    typed_receivers=typed_receivers)
    if hint is not None:
        return "db_method_call", method, hint
    return None


__all__ = [
    "classify_call", "match_api", "match_db", "is_query", "string_has_query", "text_has_query",
    "vendor_from_imports",
]
