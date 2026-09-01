"""Play controller action detection.

A Play action is a ``Function`` whose body is a call rooted at ``Action`` —
``Action { ... }`` / ``Action.async { ... }`` / ``Action(parse.json) { ... }`` /
``Action.async(parse.json) { ... }``. The verb and path live in ``conf/routes``, not the
controller, so ``method``/``endpoint`` are left honest-null here (see
``PlayRoutesParser`` in :mod:`.routes`).

Detected off the AST (the function body), falling back to a ``returnType`` prefix check
only when the body doesn't reduce to a bare ``Action`` call — inferred return types are
null in Scala far more often than in Java, so a returnType-only rule would silently miss
most actions.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import node_text

_FN_TYPES = ("function_definition", "function_declaration")


def _callee_root(node: Node | None) -> Node | None:
    """The leftmost identifier of a (possibly curried / field-access) call chain:
    ``Action.async(parse.json)`` -> the ``Action`` identifier."""
    if node is None:
        return None
    if node.type == "identifier":
        return node
    if node.type == "field_expression":
        return _callee_root(node.child_by_field_name("value"))
    if node.type in ("call_expression", "generic_function"):
        return _callee_root(node.child_by_field_name("function"))
    return None


def _is_action_call(body: Node | None, source: bytes) -> bool:
    if body is None or body.type != "call_expression":
        return False
    root = _callee_root(body.child_by_field_name("function"))
    return root is not None and node_text(root, source) == "Action"


def _is_play_action(fn_node: Node, source: bytes) -> bool:
    body = fn_node.child_by_field_name("body")
    if _is_action_call(body, source):
        return True
    ret_node = fn_node.child_by_field_name("return_type")
    if ret_node is None:
        return False
    return node_text(ret_node, source).startswith("Action")


def detect_play_actions(root: Node, source: bytes, record: FileRecord) -> list[Statement]:
    action_lines: set[int] = set()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in _FN_TYPES and _is_play_action(c, source):
                action_lines.add(c.start_point[0] + 1)
            walk(c)

    walk(root)
    if not action_lines:
        return []

    by_start = {fn.startLine: fn for fn in record.functions}
    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for start in sorted(action_lines):
        fn = by_start.get(start)
        if fn is None:
            continue
        routes.append(Statement(
            id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
            parentId=fn.id,
            nodeType="synthetic",
            semanticType="route",
            text=fn.name,
            framework="play",
            handler=fn.name,
            method=None,
            endpoint=None,
            routeKind="route",
            isRegex=False,
            startLine=fn.startLine,
            endLine=fn.endLine,
            path=fn.path,
        ))
    return routes
