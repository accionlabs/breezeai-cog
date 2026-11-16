"""PHP outbound HTTP calls (``api_call``) — recorded only where the code proves the client.

The shared classifier recognised a PHP HTTP call by a receiver *name* containing ``http``,
``guzzle``, ``curl`` … so ``$this->httpCache->get('user:1')`` became ``GET user:1`` and a Guzzle
client named ``$client`` was missed. For PHP it classifies no HTTP calls; one additive detector
per client library does, from evidence (``php/receivers.py``), with this shared loop:

=====================  =========================================================================
Client                 Proof (detector)
=====================  =========================================================================
Laravel HTTP client    ``Http::`` resolving to the imported ``Illuminate\\Support\\Facades\\Http``,
                       or a variable / property typed as ``PendingRequest`` / ``Factory``
                       (``php_laravel/http_calls.py``)
Guzzle                 a variable / property typed or constructed as ``GuzzleHttp\\Client`` /
                       ``ClientInterface`` (``php_guzzle/http_calls.py``)
Symfony HttpClient     typed as ``HttpClientInterface`` (or a concrete client), or built by
                       ``HttpClient::create()`` (``php_symfony/http_calls.py``)
PSR-18 client          typed as ``Psr\\Http\\Client\\ClientInterface`` — ``sendRequest($request)``,
                       verb and URL unknown (they are in the request object)
                       (``php_psr/http_calls.py``)
=====================  =========================================================================

A chain (``Http::withToken($t)->post($url)``) is one record, at its outermost call, and only
when it sends a request: ``get`` / ``post`` / … (``method`` = the verb), or
``request('POST', $url)`` / ``send(...)`` (``method`` from the first argument when literal).
``endpoint`` is the URL argument when it is a literal, else empty. Home-grown wrappers and
``curl_*`` are gaps, never guesses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from tree_sitter import Node

from .additive import DetectContext, Detection
from .emits import Emit
from .php.receivers import Rules, method_name, outermost_calls
from .php_route_common import args_of, string_value

VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _no_static(_fqcn: str, _method: str) -> str | None:
    return None


@dataclass(frozen=True)
class Client:
    """One client library: the types that are a client, and how it sends."""

    #: Fully qualified class → the client's kind, for declared types and ``new X()``.
    types: Mapping[str, str]
    #: Methods named after a verb (``post``), and methods taking the verb as their first
    #: argument (``request('POST', $url)``).
    named: frozenset[str]
    generic: frozenset[str]
    #: ``(class, first method)`` of a static call → kind (``Http::post`` → the Laravel client).
    static: Callable[[str, str], str | None] = field(default=_no_static)
    #: The verb and URL live in a request object, not the call (PSR-18 ``sendRequest``).
    request_object: bool = False

    def sends(self, method: str) -> bool:
        return method in self.named or method in self.generic


def _request(call: Node, client: Client, source: bytes) -> tuple[str | None, str | None]:
    """(HTTP verb, literal URL) of the call that sends."""
    method = method_name(call, source)
    args = args_of(call)
    if method in client.named:
        return method.removesuffix("async").upper(), string_value(args[0], source) if args else None
    if client.request_object:
        return None, None  # sendRequest($request): verb and URL live in the request object
    written = string_value(args[0], source) if args else None
    url = string_value(args[1], source) if len(args) > 1 else None
    return (written.upper() if written and written.lower() in VERBS else None), url


def detect_http_calls(dc: DetectContext, client: Client) -> Detection | None:
    """An ``api_call`` for every outermost chain on one of ``client``'s receivers that sends a
    request."""

    def chain_result(kind: str, methods: list[str]) -> str | None:
        """A chain that sends nothing yet is still a client (``$req = Http::withToken($t)``,
        ``$client = HttpClient::create()``); one that sends returns a response."""
        return None if any(client.sends(m) for m in methods) else kind

    rules = Rules(types=client.types, static=client.static, chain_result=chain_result)
    found: list[Emit] = []
    for call, calls, _kind in outermost_calls(dc, rules):
        senders = [c for c in calls if client.sends(method_name(c, dc.source))]
        if not senders:
            continue
        verb, url = _request(senders[-1], client, dc.source)
        found.append(Emit(call, {"semanticType": "api_call", "method": verb, "endpoint": url},
                          enrich="own"))
    return Detection(found) if found else None
