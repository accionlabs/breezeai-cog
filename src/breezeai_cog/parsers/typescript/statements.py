"""Flat statement capture for TypeScript/JavaScript (gated by --capture-statements),
with shared API/DB call detection (``parsers/detection``)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import (
    classify_statement,
    render_concat,
    resolve_endpoint,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "lexical_declaration":
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                return node_text(name, source)
    elif node.type in ("type_alias_declaration", "public_field_definition", "field_definition"):
        # `type X = …` -> X ;  class field `count = 0` -> count
        name = node.child_by_field_name("name")
        if name is not None:
            return node_text(name, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string, template literal, or ``+`` concatenation.
    Interpolations become ``{name}`` placeholders; a leading interpolated base/host
    segment is dropped (``\\`${base}/users/${id}\\``` -> ``/users/{id}``)."""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    if node.type == "template_string":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_fragment":
                parts.append(node_text(c, source))
            elif c.type == "template_substitution":
                expr = c.named_children[0] if c.named_children else None
                parts.append(url_placeholder(node_text(expr, source)) if expr is not None else "{param}")
        return strip_leading_base("".join(parts))
    if node.type == "binary_expression":  # string concatenation: '/a/' + id + '/b'
        return render_concat(node, source, _render_url)
    return None


def _resolve_args(args: Node | None, source: bytes) -> tuple[str | None, str | None]:
    """(endpoint, override_method) from a call's arguments. Handles the config-object
    form (``axios({ url, method })``) — JS-specific — then falls back to the shared
    positional resolver (first-arg / verb-first)."""
    if args is None:
        return None, None
    named = list(args.named_children)
    if not named:
        return None, None
    if named[0].type == "object":  # axios({ url: '/x', method: 'get' })
        url = override = None
        for pair in named[0].named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            val = pair.child_by_field_name("value")
            kname = node_text(key, source) if key is not None else ""
            if kname in ("url", "uri", "path") and val is not None:
                url = _render_url(val, source)
            elif kname == "method" and val is not None:
                mv = _render_url(val, source)
                override = mv.lower() if mv else None
        return url, override
    return resolve_endpoint(named, source, _render_url)


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    method = callee.rsplit(".", 1)[-1]
    endpoint, override = _resolve_args(call.child_by_field_name("arguments"), source)
    if override is not None:
        method = override
    return callee, method, endpoint


def _span(node: Node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def _iter_in_scope(node: Node, descend_all: bool = False, barriers: frozenset[tuple[int, int]] = frozenset()):
    """Yield EMIT_TYPES statement nodes. When ``descend_all`` is False (file-root /
    class-body scope) nested scopes remain barriers — they are extracted as their own
    Function/Class. When True (a function body) we descend into inline callbacks and
    lambdas, attributing their statements to this function, EXCEPT nested named
    functions (their spans are in ``barriers``): those are extracted as their own
    scope, so descending would double-emit. This closes the "callback black hole"
    while keeping one-statement-per-nearest-named-function (see build_function)."""
    for child in node.named_children:
        if _span(child) in barriers:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
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
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details, language="typescript",
            )
        )
    return out
