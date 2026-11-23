"""Ruby class / module extraction."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, Function, Statement
from ..callresolve import CallResolver
from ..treesitter import line_span, node_text
from .functions import _VISIBILITY_MARKERS, build_function
from .statement import extract_statements


_ANONYMOUS = "<anonymous>"


def _declared_path(node: Node, source: bytes) -> tuple[list[str], bool] | None:
    """The constants a ``class`` / ``module`` header names, outermost first, and whether the
    header is absolute (``class ::Top``, which ignores the enclosing scope).

    ``class Result`` -> ``(["Result"], False)``; ``class Admin::User`` ->
    ``(["Admin", "User"], False)``. ``None`` when any part is not a plain constant
    (``class foo::Bar``): the scope is then a runtime value and is not guessed.
    """
    target = node.child_by_field_name("name")
    segments: list[str] = []
    absolute = False
    while target is not None and target.type == "scope_resolution":
        name = target.child_by_field_name("name")
        if name is None or name.type != "constant":
            return None
        segments.append(node_text(name, source))
        target = target.child_by_field_name("scope")
        if target is None:
            absolute = True
    if target is not None:
        if target.type != "constant":
            return None
        segments.append(node_text(target, source))
    if not segments:
        return None
    segments.reverse()
    return segments, absolute


def _name_of_class(node: Node, source: bytes) -> str:
    """The simple name: the last constant of the header."""
    declared = _declared_path(node, source)
    return declared[0][-1] if declared is not None else _ANONYMOUS


def _qualified_name(node: Node, source: bytes, owner: str | None) -> str:
    """``Outer.Inner`` — the dotted scope an id is built from, shared with every other
    language's nested-type ids (architecture §8). A class is identified by where it is
    declared, never by its position among its siblings."""
    declared = _declared_path(node, source)
    if declared is None:
        segments, absolute = [_ANONYMOUS], False
    else:
        segments, absolute = declared
    scope = [owner] if owner and not absolute else []
    return ".".join([*scope, *segments])


def _body_of(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "body_statement"), None)


def _inline_visibility_method(call: Node, source: bytes) -> Node | None:
    if call.type != "call" or call.child_by_field_name("receiver") is not None:
        return None
    marker = call.child_by_field_name("method")
    arguments = call.child_by_field_name("arguments")
    if (
        marker is None
        or node_text(marker, source) not in _VISIBILITY_MARKERS
        or arguments is None
        or len(arguments.named_children) != 1
        or arguments.named_children[0].type != "method"
    ):
        return None
    return arguments.named_children[0]


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve_for_scope: Callable[[Node], CallResolver],
    typed_db_ids: frozenset[str] | None = None,
    owner: str | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    name = _name_of_class(node, source)
    # The id carries the enclosing scope; ``name`` and ``class_name`` stay simple.
    qualified = _qualified_name(node, source, owner)
    resolve = resolve_for_scope(node)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, qualified), seen_ids)
    body = _body_of(node)
    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []

    if body is not None:
        statements.extend(extract_statements(
            body, source, path, parent_id=cid, capture=capture, limit=limit,
            seen_ids=seen_ids, typed_db_ids=typed_db_ids,
        ))
        for child in body.named_children:
            method_node = (
                child if child.type == "method" else _inline_visibility_method(child, source)
            )
            if method_node is not None:
                fns, fn_stmts = build_function(
                    method_node,
                    source,
                    path,
                    parent_id=cid,
                    class_name=name,
                    id_owner=qualified,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                    typed_db_ids=typed_db_ids,
                )
                methods.extend(fns)
                statements.extend(fn_stmts)
            elif child.type in {"class", "module"}:
                sub_classes, sub_methods, sub_stmts = build_class(
                    child,
                    source,
                    path,
                    parent_id=cid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve_for_scope=resolve_for_scope,
                    typed_db_ids=typed_db_ids,
                    owner=qualified,
                )
                nested_classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_stmts)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="class" if node.type == "class" else "module",
        visibility="public",
        startLine=start,
        endLine=end,
    )
    return [cls, *nested_classes], methods, statements


def iter_definitions(root: Node) -> Iterator[Node]:
    for child in root.named_children:
        if child.type in {"class", "module", "method"}:
            yield child
        else:
            yield from iter_definitions(child)
