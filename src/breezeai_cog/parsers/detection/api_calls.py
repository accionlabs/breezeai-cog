"""Outbound HTTP API-call detection (language-agnostic).

Works on a normalized call: the ``callee`` text (e.g. ``axios.get``, ``this.http.post``,
``requests.get``, ``fetch``) and the ``method`` (last segment). Returns the HTTP verb
when it looks like an HTTP-client call, else ``None``.
"""

from __future__ import annotations

HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Substrings in the callee that signal an HTTP client.
_CLIENT_HINTS = (
    "axios", "http", "httpclient", "httpx", "requests", "session", "fetch",
    "restclient", "apiclient", "$http", "superagent", "got", "ky", "urllib", "aiohttp",
)


def match_api(callee: str, method: str) -> str | None:
    """Return the HTTP verb (uppercased) if ``callee.method(...)`` is an HTTP call."""
    m = method.lower()
    low = callee.lower()
    if m == "fetch" or low == "fetch" or low.endswith(".fetch"):
        return "GET"
    if m in HTTP_VERBS and any(hint in low for hint in _CLIENT_HINTS):
        return m.upper()
    return None
