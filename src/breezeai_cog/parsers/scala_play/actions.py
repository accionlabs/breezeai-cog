"""Play controller action detection and SIRD routing.

A Play action is a ``Function`` whose body is a call rooted at ``Action`` —
``Action { ... }`` / ``Action.async { ... }`` / ``Action(parse.json) { ... }`` /
``Action.async(parse.json) { ... }``. The verb and path live in ``conf/routes``, not the
controller, so ``method``/``endpoint`` are left honest-null here (see
``PlayRoutesParser`` in :mod:`.routes`).

Play SIRD (String Interpolating Routing DSL) defines routes directly in code:
``override def routes: Routes = { case GET(p"/posts/$id") => postController.show(id) }``.
"""

from __future__ import annotations

import re
from typing import Collection

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


def detect_play_actions(
    root: Node,
    source: bytes,
    record: FileRecord,
    seen_ids: set[str] | None = None,
    exclude_handlers: Collection[str] = (),
) -> list[Statement]:
    if b"Action" not in source:
        return []
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
    seen = seen_ids if seen_ids is not None else {s.id for s in record.statements}
    routes: list[Statement] = []
    for start in sorted(action_lines):
        fn = by_start.get(start)
        if fn is None:
            continue
        if fn.name in exclude_handlers:
            continue
        sid = disambiguate(statement_id(record.path, fn.startLine, 0), seen)
        routes.append(Statement(
            id=sid,
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


def _sird_path(node: Node, source: bytes) -> tuple[str, bool] | None:
    """Return the normalized path body and isRegex flag of a ``p"..."`` SIRD interpolator."""
    # B6: Must verify the interpolator prefix is specifically 'p'
    prefix = next((child for child in node.children if child.type == "identifier"), None)
    if prefix is None or node_text(prefix, source) != "p":
        return None
    literal = next(
        (child for child in node.named_children if child.type == "interpolated_string"),
        None,
    )
    raw = node_text(literal or node, source)
    if raw.startswith('"""') and raw.endswith('"""'):
        raw = raw[3:-3]
    elif raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    else:
        return None

    is_regex = bool(_SIRD_REGEX.search(raw))
    # C3: Normalize SIRD path parameters to {param}
    norm = re.sub(r"\$\{?([a-zA-Z_]\w*)\}?<[^>]+>", r"{\1}", raw)
    norm = re.sub(r"\$\{?([a-zA-Z_]\w*)\}?", r"{\1}", norm)
    norm = re.sub(r"\*([a-zA-Z_]\w*)", r"{\1}", norm)
    return norm, is_regex


def _find_router_prefix(node: Node, source: bytes) -> str | None:
    """Find ``val prefix = "/v1/posts"`` on the router class/object."""
    curr = node.parent
    while curr is not None:
        if curr.type in ("class_definition", "object_definition"):
            body = curr.child_by_field_name("body")
            if body is not None:
                for member in body.named_children:
                    if member.type in ("val_definition", "var_definition"):
                        pat = member.child_by_field_name("pattern")
                        val = member.child_by_field_name("value")
                        if pat is not None and node_text(pat, source) == "prefix" and val is not None:
                            if val.type == "string":
                                return node_text(val, source).strip('"\'')
            break
        curr = curr.parent
    return None


def _extract_sird_handler(clause: Node, source: bytes) -> str | None:
    """Extract handler name from SIRD case body: ``case GET(p"/") => controller.index`` -> ``index``."""
    body = clause.child_by_field_name("body")
    if body is None:
        # Fallback to children after '=>'
        found_arrow = False
        for c in clause.children:
            if c.type == "=>":
                found_arrow = True
            elif found_arrow and c.is_named:
                body = c
                break
    if body is None:
        return None
    if body.type == "call_expression":
        fn = body.child_by_field_name("function")
        if fn is not None:
            return node_text(fn, source).rsplit(".", 1)[-1]
    if body.type == "field_expression":
        field = body.child_by_field_name("field")
        if field is not None:
            return node_text(field, source)
        return node_text(body, source).rsplit(".", 1)[-1]
    if body.type == "identifier":
        return node_text(body, source)
    return None


def detect_play_sird_routes(
    root: Node, source: bytes, record: FileRecord, seen_ids: set[str] | None = None
) -> list[Statement]:
    """Detect Play SIRD ``Routes`` methods (``case GET(p"/path") => ...``)."""
    # D2: cheap byte guard
    if b"p\"" not in source and b"Routes" not in source and b"SimpleRouter" not in source:
        return []
    seen = seen_ids if seen_ids is not None else {s.id for s in record.statements}
    routes: list[Statement] = []

    def walk(node: Node) -> None:
        if node.type in _FN_TYPES:
            name = node.child_by_field_name("name")
            ret = node.child_by_field_name("return_type")
            body = node.child_by_field_name("body")
            # C4: Optional return type (: Routes or inferred on override)
            if (
                name is not None
                and node_text(name, source) == "routes"
                and (ret is None or node_text(ret, source).strip() == "Routes")
                and body is not None
                and body.type == "case_block"
            ):
                # C5: Mount prefix from class
                mount_prefix = _find_router_prefix(node, source)
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
                    res = _sird_path(path_node, source)
                    if res is None:
                        continue
                    endpoint, is_regex = res
                    if not endpoint.startswith("/"):
                        endpoint = "/" + endpoint
                    if mount_prefix:
                        p = "/" + mount_prefix.strip("/")
                        endpoint = p + ("/" if endpoint == "/" else endpoint)
                    # C6: Populate handler from case body
                    handler = _extract_sird_handler(clause, source)
                    start = clause.start_point[0] + 1
                    sid = disambiguate(statement_id(record.path, start, clause.start_point[1]), seen)
                    routes.append(
                        Statement(
                            id=sid,
                            parentId=find_enclosing_parent_id(start, record),
                            nodeType="synthetic",
                            semanticType="route",
                            text=node_text(clause, source),
                            framework="play",
                            handler=handler,
                            method=verb,
                            endpoint=endpoint,
                            routeKind="route",
                            isRegex=is_regex,
                            startLine=start,
                            endLine=clause.end_point[0] + 1,
                            path=record.path,
                        )
                    )
        for child in node.named_children:
            walk(child)

    walk(root)
    return routes
