"""Flat Ruby statement capture."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, emit_enum_members, render_concat, resolve_endpoint
from ..detection import text_has_query
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES

_SCOPE_TYPES = {"class", "module", "method", "singleton_method"}


def _iter_statements(node: Node, source: bytes) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in EMIT_TYPES:
            yield child
        elif child.type == "string" and node.type in {"program", "body_statement"}:
            if text_has_query(node_text(child, source)):
                yield child
        if child.type in _SCOPE_TYPES:
            continue
        yield from _iter_statements(child, source)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left is not None:
            return node_text(left, source)
    if node.type in {"class", "module"}:
        constant = next((c for c in node.named_children if c.type == "constant"), None)
        if constant is not None:
            return node_text(constant, source)
    if node.type == "method":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return node_text(ident, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    if _contains_string_interpolation(node):
        return None
    if node.type == "string":
        parts = node.named_children
        if any(part.type not in {"string_content", "escape_sequence"} for part in parts):
            return None
        return "".join(node_text(part, source) for part in parts)
    if node.type == "binary":
        return render_concat(node, source, _render_url)
    return None


def _contains_string_interpolation(node: Node) -> bool:
    """Return True when this URL expression contains Ruby string interpolation."""
    return node.type == "interpolation" or any(
        _contains_string_interpolation(child) for child in node.named_children
    )


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    receiver = call.child_by_field_name("receiver")
    method_node = call.child_by_field_name("method")
    method = node_text(method_node, source) if method_node is not None else ""
    callee = f"{node_text(receiver, source)}.{method}" if receiver is not None else method
    arguments = call.child_by_field_name("arguments")
    args = list(arguments.named_children) if arguments is not None else []
    endpoint, override = resolve_endpoint(args, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _is_enum_like_module(body: Node, source: bytes) -> bool:
    """Return True when the body looks like a Ruby enum module.

    C8 fix: detects modules used as enums (e.g. ``module Status; ACTIVE = 1; end``).
    A module body is treated as enum-like when it has at least one constant assignment
    and every non-method, non-class child is a constant assignment or a bare constant.
    Constants are SCREAMING_SNAKE_CASE (first char uppercase, all uppercase or underscore).
    """
    relevant = [
        c for c in body.named_children
        if c.type not in {"method", "class", "module", "comment"}
    ]
    if not relevant:
        return False
    constant_assigns = []
    for child in relevant:
        if child.type == "assignment":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "constant":
                name = node_text(left, source)
                if name and name[0].isupper():
                    constant_assigns.append(child)
                    continue
        # Any other node type disqualifies this module as enum-like
        return False
    return len(constant_assigns) > 0


def extract_enum_members(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    limit: int,
    seen_ids: set[str],
) -> list[Statement]:
    """Emit enum_member Statements for constant assignments in an enum-style Ruby module.

    C8 fix: constant assignments like ``ACTIVE = 1`` in enum-style modules are emitted
    as Statements with ``semanticType = "enum_member"``.
    """
    if body is None or not _is_enum_like_module(body, source):
        return []
    return emit_enum_members(
        body,
        source,
        path,
        member_types={"assignment"},
        parent_id=parent_id,
        limit=limit,
        seen_ids=seen_ids,
    )


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    typed_db_ids: frozenset[str] | None = None,
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_statements(body, source):
        if node.type not in EMIT_TYPES and not (
            node.type == "string" and text_has_query(node_text(node, source))
        ):
            continue
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
                call_type="call",
                name_of=_name_of,
                call_details=_call_details,
                language="ruby",
                typed_db_ids=typed_db_ids,
            )
        )
    return out
