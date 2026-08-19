"""Flat statement capture for Java (gated by --capture-statements) + shared API/DB
call detection."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "method_invocation"

# Declaration node types that may carry annotations (@Column, @Autowired, @NotNull, ...).
_ANNOTATED_DECL_TYPES = {"field_declaration"}


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_variable_declaration", "field_declaration"):
        decl = node.child_by_field_name("declarator")
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string literal or ``+`` concatenation (Java has no
    string interpolation); non-string parts of a concatenation become ``{name}``."""
    if node.type == "string_literal":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')
    if node.type == "binary_expression":  # "/users/" + id
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    obj = call.child_by_field_name("object")
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(named, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _field_decorators(node: Node, source: bytes) -> list[Decorator] | None:
    """Structured annotations on a field declaration, via the shared ``extract_annotations``
    (same parser used for class/method/param annotations)."""
    if node.type not in _ANNOTATED_DECL_TYPES:
        return None
    from .functions import extract_annotations, modifiers_node  # lazy — avoid an import cycle with functions.py

    return extract_annotations(modifiers_node(node), source)


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas, attributing their statements to this function; ``False``
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
                name_of=_name_of, call_details=_call_details, language="java",
                decorators=_field_decorators(node, source),
            )
        )
    return out
