"""Outbound HTTP API-call detection, shared by the languages that name their HTTP clients.

Works on a normalized call: the ``callee`` text (e.g. ``axios.get``, ``this.http.post``,
``requests.get``, ``fetch``) and the ``method`` (last segment). Returns the HTTP verb
when it looks like an HTTP-client call, else ``None``.
"""

from __future__ import annotations

# ``request`` is a generic client verb (axios.request, session.request, restTemplate…);
# it only counts when the callee also carries a client hint (below), so it can't match a
# bare ``foo.request()``.
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}

# Each language's HTTP-client names. A segment of a call's receiver is a client when its name
# ends with one of them, starting a word (:func:`_names_client`): ``dataApiClient``,
# ``DefaultHttpClient``, ``this._http``, ``axios.create()``; not ``apiClientPool`` (a pool),
# ``HttpRuntime.Cache`` (a cache) or ``httpContextProvider``. A name is read only in its own
# languages, because the same word is an ordinary name elsewhere: ``session`` is Python's
# ``requests.Session`` but a JSON object or a Map in Java and Groovy, and ``ky`` / ``got`` are JS
# libraries. A bare ``client`` is never one: it would match s3Client / dbClient / graphqlClient.
# A language with no entry (PHP, C, C++, an unknown one) has no name match at all; its HTTP
# calls are recorded only by evidence-based detectors (an import, a declared type).
_COMMON_HINTS = ("http", "httpclient", "restclient", "apiclient")
_JVM_HINTS = (*_COMMON_HINTS, "resttemplate", "webclient")
_DOTNET_HINTS = (*_COMMON_HINTS, "resttemplate", "webclient")  # Spring.NET RestTemplate
_CLIENT_HINTS: dict[str, tuple[str, ...]] = {
    "typescript": (*_COMMON_HINTS, "httpservice", "axios", "fetch", "superagent", "got", "ky",
                   "ofetch", "webclient"),
    "python": (*_COMMON_HINTS, "requests", "session", "httpx", "aiohttp", "urllib", "urllib3"),
    "java": _JVM_HINTS, "kotlin": _JVM_HINTS, "groovy": _JVM_HINTS, "scala": _JVM_HINTS,
    "csharp": _DOTNET_HINTS, "vb": _DOTNET_HINTS,
    "go": _COMMON_HINTS,
    "ruby": (*_COMMON_HINTS, "httparty"),
}


def _ends_with_word(name: str, hint: str) -> bool:
    """``name`` ends with ``hint`` (any case), and the hint starts a word there: at the start, at
    a capital (``myHttp``, ``P3APIClient``) or after a non-letter (``this._http``, ``$http``)."""
    i = len(name) - len(hint)
    if i < 0 or name[i:].lower() != hint:
        return False
    return i == 0 or name[i].isupper() or not name[i - 1].isalpha()


def _names_client(receiver: str, hints: tuple[str, ...]) -> bool:
    """Whether a segment of the receiver chain names a client (see :data:`_CLIENT_HINTS`);
    ``http_client`` is read as ``httpClient`` too, and a chain broken over lines
    (``this.http\n  .post(…)``) as one line."""
    for segment in "".join(receiver.split()).split("."):
        for name in {segment, segment.replace("_", "")}:
            if any(_ends_with_word(name, hint) for hint in hints):
                return True
    return False


#: .NET languages: ``…Async`` verbs and the ``HttpClient`` helper verbs below apply only there.
_DOTNET = frozenset({"csharp", "vb"})

# Bare (receiver-less) function calls that are HTTP requests → default GET (JS / TS only:
# Ruby's ``hash.fetch(:key)`` reads a hash).
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


#: .NET ``HttpClient`` convenience methods (``Async`` already stripped) → the verb they send.
_DOTNET_VERBS = {
    "getstring": "get", "getbytearray": "get", "getstream": "get", "getfromjson": "get",
    "postasjson": "post", "putasjson": "put", "patchasjson": "patch", "deletefromjson": "delete",
}


def match_api(
    callee: str, method: str, http_client_ids: "frozenset[str] | None" = None,
    language: str | None = None,
) -> str | None:
    """Return the HTTP verb (uppercased) if ``callee.method(...)`` is an HTTP call.

    ``http_client_ids`` is a per-file set of names known to be HTTP clients that a
    substring hint can't reach — a wrapped axios instance (``const service =
    axios.create(...)`` → ``service.get(...)``) or a config-object wrapper call
    (``request({ url, method })``), whose names are arbitrary. A callee whose receiver
    (or the bare callee itself) is in that set counts as a client, same as a hint match.
    ``language`` selects the hints (:data:`_CLIENT_HINTS`); a language without any → None.
    """
    hints = _CLIENT_HINTS.get(language or "")
    if hints is None:
        # No name match for this language: PHP's HTTP calls come from the imported Http facade
        # and typed Guzzle / Symfony clients (php_http_common.py), and C / C++ have no client
        # naming convention (``ctx_http.post("/v1/chat", handler)`` registers a server route,
        # ``sessions.get(id)`` reads a map).
        return None
    m = method.lower()
    low = callee.lower()
    if language == "typescript" and (
            m in _BARE_FUNCTIONS or low in _BARE_FUNCTIONS or low.endswith(".fetch")):
        return "GET"
    if language in _DOTNET:
        # .NET HttpClient verbs are async-suffixed (``GetAsync``/``PostAsync``…) or helpers
        # (``GetStringAsync``…); a client hint is still required, so ``FooAsync()`` never matches.
        if m.endswith("async") and len(m) > len("async"):
            m = m[: -len("async")]
        m = _DOTNET_VERBS.get(m, m)
    if m not in _HTTP_VERBS:
        return None
    if m == "request" and method != "request":
        # Capitalized Request (...) is a constructor or model type, not an outbound HTTP request call
        return None
    receiver = _receiver_text(callee)
    # In .NET a ``…Session`` is ASP.NET session state or an NHibernate ``ISession``, never an
    # HTTP client, even as the ``HttpContext.Session`` the ``http`` hint would otherwise match.
    dotnet_session = language in _DOTNET \
        and receiver.rsplit(".", 1)[-1].lower().endswith("session")
    is_client = not dotnet_session and _names_client(receiver, hints)
    if not is_client and http_client_ids:
        # names are original-case identifiers; the receiver is the segment before the first dot
        # (``service.get`` → ``service``), or the whole callee for a bare call (``request``).
        own = callee.removeprefix("this.")  # this.client.get → client
        is_client = (callee in http_client_ids or own in http_client_ids
                     or own.split(".", 1)[0] in http_client_ids)
    if is_client:
        if any(mk in low for mk in _DB_CHAIN_MARKERS):
            return None  # an ORM query chain (e.g. session.query(...).filter(...).delete())
        return m.upper()  # ``request`` -> "REQUEST" (verb lives in the config arg; matches legacy)
    return None
