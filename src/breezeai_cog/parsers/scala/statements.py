"""Flat statement capture for Scala (gated by statement capture).

Emits one Statement per matching node at every depth within the same scope. A
statement that contains a call is run through the shared detectors
(``parsers/detection``) to set ``semanticType`` (api_call / db_method_call / query_statement)
+ ``method`` / ``endpoint`` / ``dataAccessHint`` on the same span.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
import contextvars
import re
from types import MappingProxyType

from tree_sitter import Node

from ...schemas import FileRecord, Statement
from ..statements_common import (
    classify_statement,
    current_http_client_ids,
    render_concat,
    reset_http_client_ids,
    resolve_endpoint,
    set_http_client_ids,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .imports import VisibleNames
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"

#: Per-file variable name -> HTTP client type (simple name); see scoped_http_clients.
_client_types: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "scala_http_client_types", default=MappingProxyType({})
)

#: Distinctive HTTP client type names — safe to match on the bare simple name anywhere.
_SCALA_HTTP_TYPES = frozenset({
    "WSClient",
    "StandaloneWSClient",
    "HttpExt",
    "SttpBackend",
    "RestTemplate",
})
#: HTTP client types whose simple name is too common to trust (Kafka/cache/gRPC
#: wrappers, domain traits all have a ``Client``): they count only when the type, as
#: written, resolves to this FQCN — fully qualified, or a bare name the file imports.
_SCALA_HTTP_TYPES_BY_FQCN: dict[str, str] = {
    "org.http4s.client.Client": "Client",
    "java.net.http.HttpClient": "HttpClient",
}
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
#: scalaj builder methods that set the request's verb; any other builder call keeps
#: scalaj's default GET. ``method(...)`` takes the verb as an argument.
_SCALAJ_VERB_METHODS = {"postdata": "post", "postform": "post", "postmulti": "post", "put": "put"}
#: Spring RestTemplate's "ForObject"/"ForEntity" family — the verb is baked into the
#: method name rather than being the method name itself.
_REST_TEMPLATE_METHOD_VERBS = {
    "getforobject": "get",
    "getforentity": "get",
    "postforobject": "post",
    "postforentity": "post",
    "postforlocation": "post",
    "patchforobject": "patch",
    "headforheaders": "head",
    "optionsforallow": "options",
}
#: RestTemplate's generic calls, whose verb is an ``HttpMethod`` argument.
_REST_TEMPLATE_VERB_ARG_METHODS = frozenset({"exchange", "execute"})

# Bare expression-statements: Scala puts a statement-position call / infix expression
# directly under a block or template_body (no expression_statement wrapper).
_STMT_EXPR = ("call_expression", "field_expression", "infix_expression")
#: ``compilation_unit`` is the file root: a script (.sc / .mill) or a top-level
#: statement in a .scala file puts a bare call directly under it, with no enclosing
#: block. Omitting it drops most of what a script file actually contains.
_CONTAINERS = (
    "compilation_unit",
    "block",
    "indented_block",
    "template_body",
    "with_template_body",
)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("val_definition", "var_definition"):
        pat = node.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            return node_text(pat, source)
    elif node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        if left is None and node.named_children:
            left = node.named_children[0]
        if left is not None and left.type == "identifier":
            return node_text(left, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string, interpolated string (s"...", uri"..."), or + concat."""
    if node.type == "string":
        txt = node_text(node, source)
        if txt.startswith('"""') and txt.endswith('"""'):
            return txt[3:-3]
        return txt.strip('"')
    if node.type == "interpolated_string_expression":
        raw = node_text(node, source)
        quote = raw.find('"')
        if quote >= 0:
            triple = raw[quote : quote + 3] == '"""'
            body = raw[quote + (3 if triple else 1) :]
            closing = '"""' if triple else '"'
            if body.endswith(closing):
                body = body[: -len(closing)]
            body = re.sub(
                r"\$\{([^}]+)\}|\$([A-Za-z_]\w*)",
                lambda match: url_placeholder(match.group(1) or match.group(2)),
                body,
            )
            return strip_leading_base(body)
    if node.type == "infix_expression":
        op = node.child_by_field_name("operator")
        if op is not None and node_text(op, source) == "+":
            return render_concat(node, source, _render_url)
    return None


def _function_name(function: Node, source: bytes) -> str:
    raw = node_text(function, source).rsplit(".", 1)[-1]
    return raw.split("[", 1)[0]


def _chain_calls(call: Node, source: bytes) -> Iterator[Node]:
    """Yield an outer call and its receiver calls, from outermost inward."""
    current: Node | None = call
    while current is not None and current.type == _CALL_TYPE:
        yield current
        function = current.child_by_field_name("function")
        if function is not None and function.type == "generic_function":
            function = function.child_by_field_name("function") or function
        value = (
            function.child_by_field_name("value")
            if function is not None and function.type == "field_expression"
            else None
        )
        current = value if value is not None and value.type == _CALL_TYPE else None


def _call_args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    return list(args.named_children) if args.type == "arguments" else [args]


def _chain_http_method(call: Node, source: bytes) -> str | None:
    for chained in _chain_calls(call, source):
        function = chained.child_by_field_name("function")
        if function is None:
            continue
        if function.type == "generic_function":
            function = function.child_by_field_name("function") or function
        method = _function_name(function, source).lower()
        if method in _HTTP_METHODS:
            return method
    return None


def _scalaj_request(chain: Node, source: bytes) -> tuple[str, str | None]:
    """The verb and URL of a scalaj ``Http(url).….asString`` builder chain.

    The URL is ``Http``'s own argument — never another builder's (``header("Accept",
    …)``). The verb is set by the outermost (last applied) verb builder; a
    ``method(…)`` argument that isn't a literal verb leaves it unresolved (``request``).
    """
    verb: str | None = None
    endpoint: str | None = None
    for chained in _chain_calls(chain, source):
        function = chained.child_by_field_name("function")
        if function is None:
            continue
        name = _function_name(function, source)
        if name == "Http" and function.type == "identifier":
            endpoint, _ = resolve_endpoint(_call_args(chained), source, _render_url)
        elif verb is None and name.lower() in _SCALAJ_VERB_METHODS:
            verb = _SCALAJ_VERB_METHODS[name.lower()]
        elif verb is None and name == "method":
            args = _call_args(chained)
            literal = _render_url(args[0], source) if len(args) == 1 else None
            verb = literal.lower() if literal and literal.lower() in _HTTP_METHODS else "request"
    return verb or "get", endpoint


def _http_method_constant(args: list[Node], source: bytes) -> str | None:
    """The verb of an ``HttpMethod.POST`` argument, if one is written literally."""
    for arg in args:
        if arg.type != "field_expression":
            continue
        value = arg.child_by_field_name("value")
        field = arg.child_by_field_name("field")
        if value is None or field is None:
            continue
        if node_text(value, source).rsplit(".", 1)[-1] != "HttpMethod":
            continue
        verb = node_text(field, source).lower()
        if verb in _HTTP_METHODS:
            return verb
    return None


def _is_uri_or_string_arg(args: list[Node]) -> bool:
    """True when a call's sole argument is a URI/string literal (not a Request-typed value)."""
    return len(args) == 1 and args[0].type in ("string", "interpolated_string_expression")


def _chain_endpoint(call: Node, source: bytes) -> str | None:
    for chained in _chain_calls(call, source):
        endpoint, _ = resolve_endpoint(_call_args(chained), source, _render_url)
        if endpoint is not None:
            return endpoint
    return None


def _http_request_details(call: Node, source: bytes) -> tuple[str, str | None] | None:
    """Extract the verb and URI from an Akka ``HttpRequest`` in a client call."""
    stack = list(call.named_children)
    request_call: Node | None = None
    while stack:
        candidate = stack.pop()
        function = candidate.child_by_field_name("function")
        if function is None:
            stack.extend(candidate.named_children)
            continue
        if function.type == "generic_function":
            function = function.child_by_field_name("function") or function
        if candidate.type == _CALL_TYPE and _function_name(function, source) == "HttpRequest":
            request_call = candidate
            break
        stack.extend(candidate.named_children)
    if request_call is None:
        return None
    args = _call_args(request_call)
    if not args:
        return "get", None
    first_text = node_text(args[0], source)
    if args[0].type == "assignment_expression":
        value = args[0].named_children[-1] if args[0].named_children else None
        return "get", _render_url(value, source) if value is not None else None
    verb = first_text.rsplit(".", 1)[-1].lower()
    if verb in _HTTP_METHODS and len(args) >= 2:
        return verb, _render_url(args[1], source)
    return "get", _render_url(args[0], source)


def _receiver_name(callee: str) -> str:
    """Return the root receiver identifier from a Scala member-call chain."""
    return callee.split(".", 1)[0].split("(", 1)[0]


def _sttp_backend_receiver(call: Node, source: bytes) -> str | None:
    args = _call_args(call)
    if not args:
        return None
    backend = node_text(args[0], source)
    return backend if backend in current_http_client_ids() else None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    if call.type == "field_expression":
        field = call.child_by_field_name("field")
        value = call.child_by_field_name("value")
        if field is None:
            return None
        method = node_text(field, source)
        # An upper-case field is a constant read, not a paren-less call:
        # ``HttpMethods.POST`` names a verb, it doesn't send a request.
        if method.isupper():
            return None
        callee = node_text(call, source)
        chain = value if value is not None and value.type == _CALL_TYPE else None
        # scalaj's ``Http`` companion object only — a user-named ``http`` variable (e.g. a
        # config section) must not match, so this is an exact-case check, not a name hint.
        if method.lower() == "asstring" and _receiver_name(callee) == "Http" and chain:
            scalaj_verb, scalaj_endpoint = _scalaj_request(chain, source)
            return "Http", scalaj_verb, scalaj_endpoint
        endpoint = _chain_endpoint(chain, source) if chain else None
        return callee, method, endpoint

    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    callee = node_text(fn, source)
    # If generic_function like HttpRoutes.of[IO], strip type parameters for method name
    method = _function_name(fn, source)

    named_args = _call_args(call)
    endpoint, override = resolve_endpoint(named_args, source, _render_url)
    if override is not None:
        method = override

    root_receiver = _receiver_name(callee)
    lower_method = method.lower()
    if endpoint is None:
        endpoint = _chain_endpoint(call, source)

    if endpoint is not None and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", endpoint):
        if not endpoint.lower().startswith(("http:", "https:")):
            return None

    if lower_method == "singlerequest" and root_receiver.lower() == "http":
        request = _http_request_details(call, source)
        if request is not None:
            request_verb, request_endpoint = request
            return root_receiver, request_verb, request_endpoint

    # Scala clients commonly put the actual HTTP verb earlier in a builder chain:
    # ``basicRequest.get(uri"/x").send(backend)``. Re-anchor the normalized callee on
    # the typed backend so the shared detector can apply its normal receiver check.
    if lower_method == "send":
        verb = _chain_http_method(call, source)
        backend = _sttp_backend_receiver(call, source)
        if verb is not None and backend is not None:
            return f"{backend}.{verb}", verb, endpoint
        # java.net.http's ``HttpClient.send(request, handler)`` — the verb lives inside
        # the HttpRequest value, not the method name, so fall back to the sentinel.
        if root_receiver in current_http_client_ids():
            return callee, "request", endpoint

    # Spring RestTemplate: "ForObject"/"ForEntity" methods carry the verb in their name;
    # ``exchange``/``execute`` take it as an argument, read only when written as
    # ``HttpMethod.X``. Gated on the receiver being declared a ``RestTemplate`` — Play
    # WS has an ``execute`` too, whose string argument is a verb, not an endpoint.
    if _http_client_types().get(root_receiver) == "RestTemplate":
        rest_verb = _REST_TEMPLATE_METHOD_VERBS.get(lower_method)
        if rest_verb is not None:
            return callee, rest_verb, endpoint
        if lower_method in _REST_TEMPLATE_VERB_ARG_METHODS:
            return callee, _http_method_constant(named_args, source) or "request", endpoint

    # http4s terminal operations represent a GET only when the sole argument is a bare
    # Uri/String; when it's a Request value the verb lives inside that value (could be
    # anything), so fall back to the generic "request" sentinel rather than guessing GET.
    # Only typed HTTP receivers get this fallback; Quill/Neo4j/ordinary ``run`` calls
    # remain available to the existing DB detectors.
    if lower_method in {"expect", "fetchas", "run"} and root_receiver in current_http_client_ids():
        verb = "get" if _is_uri_or_string_arg(named_args) else "request"
        return callee, verb, endpoint

    # scalaj's ``Http(url).asString()`` — gated on the literal ``Http`` companion object,
    # not any name containing "http", so it doesn't generalize to string conversions.
    if lower_method == "asstring" and root_receiver == "Http" and fn.type == "field_expression":
        value = fn.child_by_field_name("value")
        if value is not None and value.type == _CALL_TYPE:
            scalaj_verb, scalaj_endpoint = _scalaj_request(value, source)
            return root_receiver, scalaj_verb, scalaj_endpoint
    return callee, method, endpoint


def collect_http_clients(types: dict[str, str], visible: VisibleNames) -> dict[str, str]:
    """Return Scala variables whose declared type is a known HTTP client/backend,
    mapped to that type's simple name."""
    clients: dict[str, str] = {}
    for name, type_text in types.items():
        written = type_text.split("<", 1)[0].split("[", 1)[0].strip()
        base = written.rsplit(".", 1)[-1]
        if base in _SCALA_HTTP_TYPES:
            clients[name] = base
            continue
        for fqcn, simple in _SCALA_HTTP_TYPES_BY_FQCN.items():
            if written == fqcn or ("." not in written and visible.resolve(written, fqcn)):
                clients[name] = simple
    return clients


@contextmanager
def scoped_http_clients(clients: Mapping[str, str]) -> Iterator[None]:
    """Make ``clients`` the file's HTTP clients — their names for the shared api-call
    classifier, their types for the Scala rules that depend on which client it is."""
    ids_token = set_http_client_ids(frozenset(clients))
    types_token = _client_types.set(MappingProxyType(dict(clients)))
    try:
        yield
    finally:
        _client_types.reset(types_token)
        reset_http_client_ids(ids_token)


def _http_client_types() -> Mapping[str, str]:
    return _client_types.get()


def _span(node: Node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def _iter_in_scope(
    node: Node,
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> Iterator[Node]:
    for child in node.named_children:
        if _span(child) in barriers:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES or (child.type in _STMT_EXPR and node.type in _CONTAINERS):
            yield child
        yield from _iter_in_scope(child, descend_all, barriers)


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
    local_names: Collection[str] = (),
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node,
                source,
                path,
                parent_id=parent_id,
                limit=limit,
                seen_ids=seen_ids,
                emit_types=EMIT_TYPES,
                control_flow=CONTROL_FLOW,
                call_type=(_CALL_TYPE, "field_expression"),
                name_of=_name_of,
                call_details=_call_details,
                stmt_expr=_STMT_EXPR,
                container_types=_CONTAINERS,
                language="scala",
                local_names=local_names,
            )
        )
    return out


def find_enclosing_parent_id(start_line: int, record: FileRecord) -> str:
    """Find the ID of the smallest function or class enclosing start_line, or fallback to record.id."""
    fn_candidates = [
        f for f in record.functions
        if f.startLine <= start_line <= f.endLine
    ]
    if fn_candidates:
        fn_candidates.sort(key=lambda f: (f.endLine - f.startLine, -f.startLine))
        return fn_candidates[0].id
    cls_candidates = [
        c for c in record.classes
        if c.startLine <= start_line <= c.endLine
    ]
    if cls_candidates:
        cls_candidates.sort(key=lambda c: (c.endLine - c.startLine, -c.startLine))
        return cls_candidates[0].id
    return record.id
