"""Flat statement capture for TypeScript/JavaScript (gated by --capture-statements),
with shared API/DB call detection (``parsers/detection``)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement
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


# HTTP verbs that may appear as the *first argument* (``http.request('GET', url)``).
_HTTP_VERB_ARGS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _placeholder(node: Node, source: bytes) -> str:
    """A non-string expression inside a URL -> ``{name}`` (or ``{param}``)."""
    simple = node_text(node, source).rsplit(".", 1)[-1]
    return "{" + simple + "}" if simple.replace("_", "").isalnum() else "{param}"


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
                parts.append(_placeholder(expr, source) if expr is not None else "{param}")
        url = "".join(parts)
        if url.startswith("{"):  # leading interpolated base/host -> keep the path only
            slash = url.find("/")
            if slash != -1:
                url = url[slash:]
        return url
    if node.type == "binary_expression":  # string concatenation: '/a/' + id + '/b'
        rendered = [_render_url(c, source) for c in node.named_children]
        if any(r is not None for r in rendered):
            return "".join(
                r if r is not None else _placeholder(c, source)
                for r, c in zip(rendered, node.named_children)
            )
    return None


def _resolve_args(args: Node | None, source: bytes) -> tuple[str | None, str | None]:
    """(endpoint, override_method) from a call's arguments.

    Handles the config-object form (``axios({ url, method })``) and the verb-first
    form (``http.request('GET', url)``); otherwise resolves the first argument."""
    if args is None:
        return None, None
    named = args.named_children
    if not named:
        return None, None
    first = named[0]
    if first.type == "object":  # axios({ url: '/x', method: 'get' })
        url = override = None
        for pair in first.named_children:
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
    if first.type == "string" and len(named) >= 2:  # request('GET', url)
        verb = _render_url(first, source)
        if verb and verb.lower() in _HTTP_VERB_ARGS:
            return _render_url(named[1], source), verb.lower()
    return _render_url(first, source), None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    method = callee.rsplit(".", 1)[-1]
    endpoint, override = _resolve_args(call.child_by_field_name("arguments"), source)
    if override is not None:
        method = override
    return callee, method, endpoint


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. When ``descend_all`` is False (file-root /
    class-body scope) nested scopes remain barriers — they are extracted as their own
    Function/Class. When True (a function body) we descend into inline callbacks,
    lambdas and any nested scope, attributing their statements to this function — a
    function body never contains a separately-extracted scope, so there is no
    double-emit (see build_function). This closes the "callback black hole"."""
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
                name_of=_name_of, call_details=_call_details,
            )
        )
    return out
