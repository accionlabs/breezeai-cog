"""Flat statement capture for Groovy (gated by statement capture) + shared API/DB
call detection."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "method_invocation"

# Named-argument keys that carry a call's URL (``get(url: "https://…", timeout: 30)``). Groovy
# collects named arguments into one map, so their position among the arguments is irrelevant.
_URL_KEYS = frozenset({"url", "uri", "path"})

# Declaration node types that may carry annotations (@Autowired, GORM constraints, ...).
_ANNOTATED_DECL_TYPES = {"field_declaration"}


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_variable_declaration", "field_declaration"):
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string literal or ``+`` concatenation. A GString
    interpolation part becomes ``{name}`` via the shared placeholder logic."""
    if node.type == "string_literal":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else node_text(node, source).strip("\"'")
    if node.type == "binary_expression":  # "/users/" + id
        return render_concat(node, source, _render_url)
    return None


def _named_url_argument(arg_nodes: list[Node], source: bytes) -> Node | None:
    """The value of a ``url:`` / ``uri:`` / ``path:`` named argument, if the call has one."""
    for arg in arg_nodes:
        if arg.type != "named_argument":
            continue
        key = arg.child_by_field_name("key")
        if key is not None and node_text(key, source).strip("'\"") in _URL_KEYS:
            return arg.child_by_field_name("value")
    return None


# A member access: ``a.b``, ``a?.b`` (safe navigation) or ``a*.b`` (spread), as (object field,
# member field).
_MEMBER_ACCESS = {
    "field_access": ("object", "field"),
    "safe_navigation_expression": ("object", "property"),
    "spread_dot_expression": ("object", "property"),
}


def _receiver_text(node: Node, source: bytes) -> str:
    """A call's receiver as source text, without the bodies of closures passed along the
    chain: ``ids.collect { it.repo }.findAll`` reads ``ids.collect.findAll``, so a name inside
    a closure never looks like the receiver. Each member access is joined with ``.``; a call
    keeps its argument list (``split(';')``)."""
    fields = _MEMBER_ACCESS.get(node.type)
    if fields is not None:
        obj = node.child_by_field_name(fields[0])
        member = node.child_by_field_name(fields[1])
        name = node_text(member, source) if member is not None else ""
        return f"{_receiver_text(obj, source)}.{name}" if obj is not None else name
    if node.type == _CALL_TYPE:
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        head = _receiver_text(fn, source) if fn is not None else ""
        return head + (node_text(args, source) if args is not None else "")
    return node_text(node, source)


def only_closure_argument(call: Node) -> bool:
    """True when a call's one argument is a closure: ``items.findAll { it.active }`` or
    ``items.findAll({ it.active })``."""
    args = call.child_by_field_name("arguments")
    arg_nodes = list(args.named_children) if args is not None else []
    if call.child_by_field_name("closure") is not None:
        return not arg_nodes
    return len(arg_nodes) == 1 and arg_nodes[0].type == "closure"


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "field_access":
        field = fn.child_by_field_name("field")
        method = node_text(field, source) if field is not None else ""
        callee = _receiver_text(fn, source)
    else:
        method = node_text(fn, source)
        callee = method
    args = call.child_by_field_name("arguments")
    arg_nodes = list(args.named_children) if args is not None else []
    url_arg = _named_url_argument(arg_nodes, source)
    if url_arg is not None:
        endpoint, override = _render_url(url_arg, source), None
    else:
        positional = [a for a in arg_nodes if a.type != "named_argument"]
        endpoint, override = resolve_endpoint(positional, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _field_decorators(node: Node, source: bytes) -> list[Decorator] | None:
    """Structured annotations on a field declaration, via the shared ``extract_annotations``
    (same parser used for class/method/param annotations)."""
    if node.type not in _ANNOTATED_DECL_TYPES:
        return None
    from .functions import extract_annotations  # lazy — avoid an import cycle with functions.py

    return extract_annotations(node, source)


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline closures, attributing their statements to this function; ``False``
    (file-root / class-body) keeps nested scopes as barriers since they are extracted
    as their own Function/Class."""
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _iter_in_scope(child, descend_all)


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
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all):
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details, language="groovy",
                closure_only=only_closure_argument,
                decorators=_field_decorators(node, source),
            )
        )
    return out
