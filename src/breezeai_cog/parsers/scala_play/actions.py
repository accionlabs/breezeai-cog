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

import re

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..scala.statements import find_enclosing_parent_id
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
    ret = node_text(ret_node, source)
    return ret == "Action" or ret.startswith("Action[")


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
            id=disambiguate(statement_id(record.path, fn.startLine, 0), seen),
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


_SIRD_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_SIRD_REGEX = re.compile(r"\$[^/]+<[^>]+>")


def _sird_path(node: Node, source: bytes) -> str | None:
    """Return the literal body of a ``p"..."`` SIRD interpolator."""
    literal = next(
        (child for child in node.named_children if child.type == "interpolated_string"),
        None,
    )
    text = node_text(literal or node, source)
    if text.startswith('"""') and text.endswith('"""'):
        return text[3:-3]
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return None


def detect_play_sird_routes(root: Node, source: bytes, record: FileRecord) -> list[Statement]:
    """Detect Play SIRD ``Routes`` methods (``case GET(p"/path") => ...``)."""
    seen = {s.id for s in record.statements}
    routes: list[Statement] = []

    def walk(node: Node) -> None:
        if node.type in _FN_TYPES:
            name = node.child_by_field_name("name")
            ret = node.child_by_field_name("return_type")
            body = node.child_by_field_name("body")
            if (
                name is not None
                and node_text(name, source) == "routes"
                and ret is not None
                and node_text(ret, source).strip() == "Routes"
                and body is not None
                and body.type == "case_block"
            ):
                for clause in body.named_children:
                    if clause.type != "case_clause":
                        continue
                    pattern = clause.child_by_field_name("pattern")
                    if pattern is None or pattern.type != "case_class_pattern":
                        continue
                    children = list(pattern.named_children)
                    if len(children) < 2:
                        continue
                    verb = node_text(children[0], source).upper()
                    path_node = children[1]
                    if verb not in _SIRD_VERBS or path_node.type != "interpolated_string_expression":
                        continue
                    endpoint = _sird_path(path_node, source)
                    if endpoint is None:
                        continue
                    if not endpoint.startswith("/"):
                        endpoint = "/" + endpoint
                    start = clause.start_point[0] + 1
                    routes.append(
                        Statement(
                            id=disambiguate(
                                statement_id(record.path, start, clause.start_point[1]), seen
                            ),
                            parentId=find_enclosing_parent_id(start, record),
                            nodeType="synthetic",
                            semanticType="route",
                            text=node_text(clause, source),
                            framework="play",
                            handler=None,
                            method=verb,
                            endpoint=endpoint,
                            routeKind="route",
                            isRegex=bool(_SIRD_REGEX.search(endpoint)),
                            startLine=start,
                            endLine=clause.end_point[0] + 1,
                            path=record.path,
                        )
                    )
        for child in node.named_children:
            walk(child)

    walk(root)
    return routes
