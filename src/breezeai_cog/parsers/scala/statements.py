"""Flat statement capture for Scala (gated by --capture-statements).

Emits one Statement per matching node at every depth within the same scope. A
statement that contains a call is run through the shared detectors
(``parsers/detection``) to set ``semanticType`` (api_call / db_method_call / query_statement)
+ ``method`` / ``endpoint`` / ``dataAccessHint`` on the same span.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
import re

from tree_sitter import Node

from ...schemas import FileRecord, Statement
from ..statements_common import (
    classify_statement,
    current_http_client_ids,
    render_concat,
    resolve_endpoint,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"

_SCALA_HTTP_TYPES = frozenset({
    "WSClient",
    "StandaloneWSClient",
    "Client",
    "HttpExt",
    "SttpBackend",
})
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

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
        callee = node_text(call, source)
        endpoint = (
            _chain_endpoint(value, source)
            if value is not None and value.type == _CALL_TYPE
            else None
        )
        if method.lower() == "asstring" and _receiver_name(callee).lower() == "http":
            return "Http", "get", endpoint
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

    # http4s terminal operations represent a GET when no earlier verb is present. Only
    # typed HTTP receivers get this fallback; Quill/Neo4j/ordinary ``run`` calls remain
    # available to the existing DB detectors.
    if lower_method in {"expect", "fetchas", "run"} and root_receiver in current_http_client_ids():
        return callee, "get", endpoint

    # scalaj's ``Http(url).asString`` is a GET-shaped request whose receiver itself carries
    # the HTTP hint. Do not generalize ``asString`` to arbitrary string conversions.
    if lower_method == "asstring" and root_receiver.lower() == "http":
        return root_receiver, "get", endpoint
    return callee, method, endpoint


def collect_http_client_ids(types: dict[str, str]) -> frozenset[str]:
    """Return Scala variables whose declared type is a known HTTP client/backend."""
    ids: set[str] = set()
    for name, type_text in types.items():
        base = type_text.split("<", 1)[0].split("[", 1)[0].strip().rsplit(".", 1)[-1]
        if base in _SCALA_HTTP_TYPES:
            ids.add(name)
    return frozenset(ids)


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
