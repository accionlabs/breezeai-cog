"""Outbound HTTP API-call detection (language-agnostic).

Works on a normalized call: the ``callee`` text (e.g. ``axios.get``, ``this.http.post``,
``requests.get``, ``fetch``) and the ``method`` (last segment). Returns the HTTP verb
when it looks like an HTTP-client call, else ``None``.
"""

from __future__ import annotations

# ``request`` is a generic client verb (axios.request, session.request, restTemplate…);
# it only counts when the callee also carries a client hint (below), so it can't match a
# bare ``foo.request()``.
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}

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


def _receiver_text(callee: str) -> str:
    """Return the receiver portion of a normalized callee, without call arguments.

    The old detector searched the entire callee, which let an inline URL such as
    ``ws.url("https://example.test")`` make a Play WS call look like an HTTP client.
    Keeping only receiver text preserves hints in ``this.http.post`` while excluding
    strings and argument expressions from the signal.
    """
    visible: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    for char in callee:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif depth == 0:
            visible.append(char)
    return "".join(visible).rsplit(".", 1)[0]


def match_api(
    callee: str, method: str, http_client_ids: "frozenset[str] | None" = None
) -> str | None:
    """Return the HTTP verb (uppercased) if ``callee.method(...)`` is an HTTP call.

    ``http_client_ids`` is a per-file set of names known to be HTTP clients that a
    substring hint can't reach — a wrapped axios instance (``const service =
    axios.create(...)`` → ``service.get(...)``) or a config-object wrapper call
    (``request({ url, method })``), whose names are arbitrary. A callee whose receiver
    (or the bare callee itself) is in that set counts as a client, same as a hint match.
    """
    m = method.lower()
    low = callee.lower()
    if m in _BARE_FUNCTIONS or low in _BARE_FUNCTIONS or low.endswith(".fetch"):
        return "GET"
    # .NET (C#/VB) HttpClient uses async-suffixed verbs (``GetAsync``/``PostAsync``…);
    # strip a trailing ``async`` so they match the same verbs (precision preserved — a
    # client-hint substring is still required, so a bare ``FooAsync()`` never matches).
    if m.endswith("async") and len(m) > len("async"):
        m = m[: -len("async")]
    if m not in _HTTP_VERBS:
        return None
    if m == "request" and method != "request":
        # Capitalized Request (...) is a constructor or model type, not an outbound HTTP request call
        return None
    is_client = any(hint in _receiver_text(low) for hint in _CLIENT_HINTS)
    if not is_client and http_client_ids:
        # names are original-case identifiers; the receiver is the segment before the first dot
        # (``service.get`` → ``service``), or the whole callee for a bare call (``request``).
        is_client = callee in http_client_ids or callee.split(".", 1)[0] in http_client_ids
    if is_client:
        if any(mk in low for mk in _DB_CHAIN_MARKERS):
            return None  # an ORM query chain (e.g. session.query(...).filter(...).delete())
        return m.upper()  # ``request`` -> "REQUEST" (verb lives in the config arg; matches legacy)
    return None
