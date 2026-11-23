"""Check emitted entry points against the entry-point contract (``docs/architecture/entry-points.md``).

:func:`validate` returns the contract violations in one file record. It is a check, never a
transform: it runs in tests, in golden comparisons and under ``--verbose`` (a ``contract.violation``
debug line per finding), and never changes or filters output.

A violation is an **error** (a value that is wrong) or a **gap** (a value that is missing). Wrong
is worse than missing (§8): a parser leaves a handler empty when it cannot name it for certain
(a factory call's result, a page made of two components), exactly as it leaves an unresolved
``endpoint`` empty. Tests fail on errors; gaps are counted, as a completeness measure.

The entry points are the ``route``, ``eventbus_consumer`` and ``timer`` records whose
``routeKind`` is not ``field_resolver``, ``mount``, ``layout`` or ``navigation`` (contract §5).
Wiring has no record, except a mount whose routes aren't in the repo (``route`` / ``mount``, the
Target Spec's current form: its address is recorded nowhere else); ``layout`` is no longer
written. Neither is an entry point. A link (``route`` / ``navigation``) is outbound and checked
like one.

=============== ======== =================================================================
Rule            Severity Violation
=============== ======== =================================================================
``method``      error    ``method`` outside the contract's value set (§5)
``routeKind``   error    missing on a ``route``, or outside the value set for its semantic
                         type (§5)
``framework``   error    missing (§5)
``endpoint``    error    an empty string instead of no value, or source text instead of an
                         address (``${a + b}``, ``#{…}``, a backtick); a value read at
                         runtime written ``${name}`` is the contract's marker (§6), not one
``one-record``  error    two entry points with the same file, line, method, endpoint,
                         handler and version (§8); one address serving several handlers (a
                         Vert.x service proxy's methods) or several header-selected versions
                         is several bindings
``guards``      error    an empty list, an empty name or a repeated name (§5)
``handlerPath`` error    an empty string, the record's own file, or an absolute path (§5)
``handler``     gap      neither ``handler`` nor ``handlerLine``, except for a URL served
                         by framework code — health checks, GraphQL transports and UIs,
                         Swagger UI, static files, WebJars, Relay ``node`` fields (§4, §8
                         "every record leads to its handler")
``handlerLine`` gap      a named ``handler`` with no ``handlerLine``: declared elsewhere and
                         not located (no ``handlerPath``), so the record cannot lead to it —
                         except a view Django serves itself (``TemplateView``, ``serve``)
``outbound``    error    an outbound record (``api_call``, a ``route`` link with
                         ``routeKind: navigation``, ``eventbus_send`` / ``_publish``,
                         ``mcp_call``) whose ``endpoint`` is an empty
                         string or a code placeholder — it must be written like the entry
                         point it reaches (§6); an ``mcp_call`` also needs a ``method`` of
                         ``TOOL`` / ``RESOURCE`` / ``PROMPT``, a ``framework`` and an
                         ``endpoint``, and has no ``handler`` / ``handlerLine``
=============== ======== =================================================================
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ..schemas import FileRecord, Statement
from .aspnet_common import FRAMEWORK_SERVED
from .django_common import DJANGO_SERVED

#: ``mcp_tool`` / ``mcp_resource`` / ``mcp_prompt``: what an MCP server exposes is an entry point
#: an AI client calls, reads or fetches, held to the same contract as a route
#: (``method`` = ``TOOL`` / ``RESOURCE`` / ``PROMPT``; ``endpoint`` = the tool name, the resource
#: URI or URI template, the prompt name).
ENTRY_POINT_TYPES = frozenset(
    {"route", "eventbus_consumer", "timer", "mcp_tool", "mcp_resource", "mcp_prompt"}
)

#: ``routeKind`` values filed under ``route`` that are not entry points (see the docstring).
NOT_ENTRY_POINT_KINDS = frozenset({"field_resolver", "mount", "layout", "navigation"})

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
METHODS = HTTP_METHODS | frozenset({
    "TRACE", "CONNECT",  # HTTP verbs few frameworks route (Vert.x RouteMatcher / Router)
    "ANY", "WEBSOCKET",
    "QUERY", "MUTATION", "SUBSCRIPTION", "RESOLVE_FIELD", "RESOLVE_REFERENCE",
    "RPC",
    "MESSAGE", "EVENT", "CONSUMER",
    "TIMER", "PERIODIC",
    "TOOL", "RESOURCE", "PROMPT",
})

ROUTE_KINDS: dict[str, frozenset[str | None]] = {
    "route": frozenset({"route", "rpc", "ws", "page", "query", "mutation", "subscription"}),
    "eventbus_consumer": frozenset({None, "message"}),
    "timer": frozenset({None}),
    "mcp_tool": frozenset({None}),
    "mcp_resource": frozenset({None}),
    "mcp_prompt": frozenset({None}),
}

_PLACEHOLDERS = ("${", "#{", "`")
#: A part read at runtime, as the contract writes it (``${contextRoot}``; addresses.py).
_RUNTIME_PART = re.compile(r"\$\{[\w.-]*\}")

#: An RFC 6570 expression of a URI template (``{id}``, ``{+path}``, ``{#anchor}``, ``{ids*}``),
#: not one written as ``${…}``: a resource URI template's ``page://doc#{anchor}`` is a literal
#: ``#`` and an expression, not a ``#{…}`` placeholder.
_URI_TEMPLATE_EXPRESSION = re.compile(r"(?<!\$)\{[^{}]*\}")

#: Rules whose violation is a missing value, not a wrong one.
_GAPS = frozenset({"handler", "handlerLine"})


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    #: ``error`` (a wrong value) or ``gap`` (a missing one).
    severity: str
    statement_id: str
    detail: str


#: Outbound semantic types: their address is written like the entry point they reach (§6).
OUTBOUND_TYPES = frozenset({"api_call", "eventbus_send", "eventbus_publish", "mcp_call"})

#: What an MCP client call reaches: a tool, a resource or a prompt.
MCP_CALL_METHODS = frozenset({"TOOL", "RESOURCE", "PROMPT"})


def _bad_address(s: Statement) -> str | None:
    """Why ``s.endpoint`` is not an address, or None. An MCP resource URI is read without its
    RFC 6570 expressions. An MCP name or URI is a literal, so only a non-MCP address may hold a
    ``${name}`` part read at runtime."""
    endpoint = s.endpoint
    if endpoint == "":
        return "empty string; an unresolved address has no value"
    if endpoint is None:
        return None
    literal = _URI_TEMPLATE_EXPRESSION.sub("", endpoint) if _is_resource_uri(s) else endpoint
    if not _is_mcp(s):
        literal = _RUNTIME_PART.sub("", literal)
    if any(p in literal for p in _PLACEHOLDERS):
        return endpoint
    return None


def _is_mcp(s: Statement) -> bool:
    """An MCP server's tool, resource or prompt, or an MCP client's call."""
    return s.semanticType in {"mcp_tool", "mcp_resource", "mcp_prompt", "mcp_call"}


def _is_resource_uri(s: Statement) -> bool:
    """An MCP resource's URI or URI template: an ``mcp_resource``, or an ``mcp_call`` reading
    one."""
    return s.semanticType == "mcp_resource" or (
        s.semanticType == "mcp_call" and s.method == "RESOURCE"
    )


def _check_mcp_call(s: Statement) -> list[str]:
    """What an ``mcp_call`` lacks or must not have: it names what it calls (a literal tool name,
    URI or prompt name), the kind and the client SDK, and has no handler of its own."""
    found: list[str] = []
    if s.method not in MCP_CALL_METHODS:
        found.append(f"method {s.method!r}")
    if not s.framework:
        found.append("framework missing")
    if s.endpoint is None:
        found.append("endpoint missing")
    if s.handler is not None or s.handlerLine is not None:
        found.append("a client call has no handler")
    return found


def is_entry_point(s: Statement) -> bool:
    return s.semanticType in ENTRY_POINT_TYPES and s.routeKind not in NOT_ENTRY_POINT_KINDS


#: Framework code that answers a mounted path itself (Express middleware, Play's WebJars router).
_SERVING_MIDDLEWARE = ("expressMiddleware", "swaggerUi.", "express.static", "serveStatic",
                       "graphqlHTTP", "createHandler", "webjars.Routes")


def _django_served(s: Statement) -> bool:
    """A Django URL pattern whose view is Django's own (``TemplateView.as_view(…)``, ``serve``):
    named, with nothing in the repo to locate."""
    return (s.framework in ("django", "drf") and s.handler in DJANGO_SERVED
            and s.handlerLine is None and s.handlerPath is None)


def _framework_served(s: Statement) -> bool:
    if any(name in s.text for name in (*FRAMEWORK_SERVED, *_SERVING_MIDDLEWARE)):
        return True
    if _django_served(s):
        return True
    # A Next.js MDX page: the file's content is the page, no named component handles it.
    if (s.framework == "nextjs" and s.routeKind == "page" and s.nodeType == "synthetic"
            and s.parentId.lower().endswith(".mdx")):
        return True
    # A Relay node field of a server's schema: the GraphQL server resolves it.
    return (s.framework == "graphql" and s.routeKind == "query" and s.responseDTO == "Node"
            and s.endpoint in ("node", "nodes"))


def _check(s: Statement) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if s.method is not None and s.method not in METHODS:
        found.append(("method", s.method))
    kinds = ROUTE_KINDS[s.semanticType or ""]
    if s.routeKind not in kinds:
        found.append(("routeKind", repr(s.routeKind)))
    if not s.framework:
        found.append(("framework", "missing"))
    bad = _bad_address(s)
    if bad is not None:
        found.append(("endpoint", bad))
    if not s.handler and s.handlerLine is None and not _framework_served(s):
        found.append(("handler", "no handler and no handlerLine"))
    if s.handler and s.handlerLine is None and not _django_served(s):
        found.append(("handlerLine", f"{s.handler!r} not located"))
    if s.handlerPath is not None and (
        not s.handlerPath or s.handlerPath == s.path or s.handlerPath.startswith("/")
    ):
        found.append(("handlerPath", repr(s.handlerPath)))
    if s.guards is not None and (
        not s.guards or not all(s.guards) or len(set(s.guards)) != len(s.guards)
    ):
        found.append(("guards", repr(s.guards)))
    return found


def validate(record: FileRecord) -> list[Violation]:
    """Contract violations among ``record``'s entry points, in statement order."""
    out: list[Violation] = []
    seen: Counter[tuple[int, str | None, str | None, str | None, str | None]] = Counter()
    for s in record.statements:
        if s.semanticType in OUTBOUND_TYPES or s.routeKind == "navigation":
            bad = _bad_address(s)
            if bad is not None:
                out.append(Violation("outbound", "error", s.id, bad))
            if s.semanticType == "mcp_call":
                out.extend(Violation("outbound", "error", s.id, d) for d in _check_mcp_call(s))
            continue
        if not is_entry_point(s):
            continue
        out.extend(
            Violation(rule, "gap" if rule in _GAPS else "error", s.id, detail)
            for rule, detail in _check(s)
        )
        key = (s.startLine, s.method, s.endpoint, s.handler, s.version)
        seen[key] += 1
        if seen[key] == 2:
            out.append(Violation(
                "one-record", "error", s.id,
                f"line {s.startLine} {s.method} {s.endpoint!r} recorded twice",
            ))
    return out
