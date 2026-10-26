"""PHP outbound HTTP calls (``api_call``) — recorded only where the code proves the client.

The shared classifier recognised a PHP HTTP call by a receiver *name* containing ``http``,
``guzzle``, ``curl`` … so ``$this->httpCache->get('user:1')`` became ``GET user:1`` and a Guzzle
client named ``$client`` was missed. For PHP it classifies no HTTP calls; this additive
detector does, from evidence (``php/receivers.py``):

=====================  =========================================================================
Client                 Proof
=====================  =========================================================================
Laravel HTTP client    ``Http::`` resolving to the imported ``Illuminate\\Support\\Facades\\Http``,
                       or a variable / property typed as ``PendingRequest`` / ``Factory``
Guzzle                 a variable / property typed or constructed as ``GuzzleHttp\\Client`` /
                       ``ClientInterface``
Symfony HttpClient     typed as ``HttpClientInterface`` (or a concrete client), or built by
                       ``HttpClient::create()``
PSR-18 client          typed as ``Psr\\Http\\Client\\ClientInterface`` — ``sendRequest($request)``,
                       verb and URL unknown (they are in the request object)
=====================  =========================================================================

A chain (``Http::withToken($t)->post($url)``) is one record, at its outermost call, and only
when it sends a request: ``get`` / ``post`` / … (``method`` = the verb), or
``request('POST', $url)`` / ``send(...)`` (``method`` from the first argument when literal).
``endpoint`` is the URL argument when it is a literal, else empty. Home-grown wrappers and
``curl_*`` are gaps, never guesses.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..php_route_common import args_of, string_value
from .receivers import Rules, method_name, outermost_calls

_TYPES = {
    "GuzzleHttp\\Client": "guzzle",
    "GuzzleHttp\\ClientInterface": "guzzle",
    "Symfony\\Contracts\\HttpClient\\HttpClientInterface": "symfony",
    "Symfony\\Component\\HttpClient\\CurlHttpClient": "symfony",
    "Symfony\\Component\\HttpClient\\NativeHttpClient": "symfony",
    "Symfony\\Component\\HttpClient\\ScopingHttpClient": "symfony",
    "Illuminate\\Http\\Client\\PendingRequest": "laravel",
    "Illuminate\\Http\\Client\\Factory": "laravel",
    "Psr\\Http\\Client\\ClientInterface": "psr",
}
_MARKERS = (b"Illuminate\\Support\\Facades\\Http", b"Illuminate\\Http\\Client", b"GuzzleHttp\\",
            b"Symfony\\Contracts\\HttpClient", b"Symfony\\Component\\HttpClient", b"Psr\\Http\\Client")

_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
#: kind → (methods named after a verb, methods taking the verb as their first argument).
_SENDS: dict[str, tuple[set[str], set[str]]] = {
    "laravel": (_VERBS - {"options"}, {"send"}),
    "guzzle": (_VERBS | {f"{verb}async" for verb in _VERBS}, {"request", "requestasync"}),
    "symfony": (set(), {"request"}),
    "psr": (set(), {"sendrequest"}),
}


def _static(fqcn: str, method: str) -> str | None:
    if fqcn == "Illuminate\\Support\\Facades\\Http":
        return "laravel"
    if fqcn == "Symfony\\Component\\HttpClient\\HttpClient":
        return "symfony" if method in {"create", "createforbaseuri"} else None
    return None


def _sends(kind: str, method: str) -> bool:
    named, generic = _SENDS[kind]
    return method in named or method in generic


def _chain_result(kind: str, methods: list[str]) -> str | None:
    """A chain that sends nothing yet is still a client (``$req = Http::withToken($t)``,
    ``$client = HttpClient::create()``); one that sends returns a response."""
    return None if any(_sends(kind, m) for m in methods) else kind


def _request(call: Node, kind: str, source: bytes) -> tuple[str | None, str | None]:
    """(HTTP verb, literal URL) of the call that sends."""
    method = method_name(call, source)
    args = args_of(call)
    named, _ = _SENDS[kind]
    if method in named:
        return method.removesuffix("async").upper(), string_value(args[0], source) if args else None
    if kind == "psr":
        return None, None  # sendRequest($request): verb and URL live in the request object
    written = string_value(args[0], source) if args else None
    url = string_value(args[1], source) if len(args) > 1 else None
    return (written.upper() if written and written.lower() in _VERBS else None), url


def detect_php_http_calls(dc: DetectContext) -> Detection | None:
    if not any(marker in dc.source for marker in _MARKERS):
        return None
    rules = Rules(types=_TYPES, static=_static, chain_result=_chain_result)
    found: list[Emit] = []
    for call, calls, kind in outermost_calls(dc, rules):
        senders = [c for c in calls if _sends(kind, method_name(c, dc.source))]
        if not senders:
            continue
        verb, url = _request(senders[-1], kind, dc.source)
        found.append(Emit(call, {"semanticType": "api_call", "method": verb, "endpoint": url},
                          enrich="own"))
    return Detection(found) if found else None


register_detector(Detector(
    name="php-http-calls",
    gate=Gate("php", order=50),
    detect=detect_php_http_calls,
))
