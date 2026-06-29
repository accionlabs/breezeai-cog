"""Shared, cross-language detectors (the value that lived in the old ``utils.js`` /
``routes-js-core.js``). Per-language parsers extract a normalized call and these
classify it — so API/DB detection isn't reimplemented per language.
"""

from __future__ import annotations

from .api_calls import match_api
from .db_queries import match_db


def classify_call(callee: str, method: str) -> tuple[str, str, str | None] | None:
    """Classify a normalized call into ``(semanticType, method, dataAccessHint)``.

    * HTTP client call -> ``("api_call", "<VERB>", None)``
    * ORM/DB call      -> ``("db_method_call", "<method>", "<hint>")``
    * otherwise        -> ``None``

    API is tried first (so ``objects.get`` is a Django query, not an HTTP GET).
    """
    verb = match_api(callee, method)
    if verb is not None:
        return "api_call", verb, None
    hint = match_db(callee, method)
    if hint is not None:
        return "db_method_call", method, hint
    return None


__all__ = ["classify_call", "match_api", "match_db"]
