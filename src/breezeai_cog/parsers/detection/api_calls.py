"""Outbound HTTP API-call detection (language-agnostic).

Works on a normalized call: the ``callee`` text (e.g. ``axios.get``, ``this.http.post``,
``requests.get``, ``fetch``) and the ``method`` (last segment). Returns the HTTP verb
when it looks like an HTTP-client call, else ``None``.
"""

from __future__ import annotations

# ``request`` is a generic client verb (axios.request, session.request, restTemplate…);
# it only counts when the callee also carries a client hint (below), so it can't match a
# bare ``foo.request()``.
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}

# Substrings in the callee that signal an HTTP client (matches JS ``API_CLIENT_NAMES``,
# minus a bare ``client`` which would over-match s3Client/dbClient/graphqlClient as
# substrings — precision-preserving deviation).
_CLIENT_HINTS = (
    "axios", "http", "httpclient", "httpservice", "httpx", "requests", "session", "fetch",
    "restclient", "apiclient", "resttemplate", "webclient", "$http", "superagent",
    "got", "ky", "urllib", "aiohttp", "guzzle", "ofetch",
)

# Bare (receiver-less) function calls that are HTTP requests → default GET.
_BARE_FUNCTIONS = {"fetch", "$fetch", "usefetch", "apifetch", "authfetch", "customfetch"}

# ORM query-builder markers that make a chain a DB query, not an HTTP call — the
# ``session`` client hint collides with SQLAlchemy's DB session, so a chain like
# ``session.query(User).filter(...).delete()`` ends in an HTTP verb but is data access.
# These appear in the *callee* (the method chain), never in call arguments.
_DB_CHAIN_MARKERS = ("query(", ".filter", ".where", "query.")


def match_api(callee: str, method: str) -> str | None:
    """Return the HTTP verb (uppercased) if ``callee.method(...)`` is an HTTP call."""
    m = method.lower()
    low = callee.lower()
    if m in _BARE_FUNCTIONS or low in _BARE_FUNCTIONS or low.endswith(".fetch"):
        return "GET"
    # .NET (C#/VB) HttpClient uses async-suffixed verbs (``GetAsync``/``PostAsync``…);
    # strip a trailing ``async`` so they match the same verbs (precision preserved — a
    # client-hint substring is still required, so a bare ``FooAsync()`` never matches).
    if m.endswith("async") and len(m) > len("async"):
        m = m[: -len("async")]
    if m in HTTP_VERBS and any(hint in low for hint in _CLIENT_HINTS):
        if any(mk in low for mk in _DB_CHAIN_MARKERS):
            return None  # an ORM query chain (e.g. session.query(...).filter(...).delete())
        return m.upper()  # ``request`` -> "REQUEST" (verb lives in the config arg; matches legacy)
    return None
