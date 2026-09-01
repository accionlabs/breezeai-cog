"""http4s route detection for ``HttpRoutes.of`` / ``AuthedRoutes.of`` case-pattern matching.

In http4s, routes are defined using pattern matching inside ``HttpRoutes.of[F] { case ... }``:
- Patterns chain left-associatively: ``GET -> Root / "users" / id``
- The base of the pattern is ``VERB -> Root`` (with optional capture pattern ``req @ VERB -> Root``)
- Segments wrap with ``/``: string literals stay literal, identifiers and extractors become ``{param}``
- Queries (``:?``) and auth wrappers (``as user``) are unwrapped cleanly
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..statements_common import url_placeholder
from ..treesitter import node_text

_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def _unwrap_verb(node: Node | None, source: bytes) -> str | None:
    """Extract HTTP verb name from an identifier or capture pattern (``req @ POST``)."""
    if node is None:
        return None
    if node.type == "identifier":
        name = node_text(node, source).upper()
        if name in _HTTP_VERBS:
            return name
    if node.type == "capture_pattern":
        pat = node.child_by_field_name("pattern")
        return _unwrap_verb(pat, source)
    return None


def _extract_http4s_pattern(pat_node: Node | None, source: bytes) -> tuple[str, list[str]] | None:
    """Extract (verb, list_of_segments) from a http4s case pattern, or None if not a route pattern."""
    if pat_node is None:
        return None

    # Unpack auth wrapper in AuthedRoutes: ``case GET -> Root / "me" as user =>``
    if pat_node.type == "infix_pattern":
        op = pat_node.child_by_field_name("operator")
        if op is not None and node_text(op, source) == "as":
            return _extract_http4s_pattern(pat_node.child_by_field_name("left"), source)

    segments: list[str] = []
    curr: Node | None = pat_node

    while curr is not None and curr.type == "infix_pattern":
        op = curr.child_by_field_name("operator")
        op_text = node_text(op, source) if op is not None else ""

        if op_text == "/":
            right = curr.child_by_field_name("right")
            if right is not None:
                if right.type == "string":
                    seg = node_text(right, source).strip('"\'')
                    segments.append(seg)
                elif right.type == "identifier":
                    segments.append(url_placeholder(node_text(right, source)))
                elif right.type == "case_class_pattern":
                    # Extractor pattern e.g. IntVar(itemId) -> extract inner pattern name
                    pat_child = right.child_by_field_name("pattern")
                    if pat_child is not None and pat_child.type == "identifier":
                        segments.append("{" + node_text(pat_child, source) + "}")
                    else:
                        segments.append(url_placeholder(node_text(right, source)))
                else:
                    segments.append(url_placeholder(node_text(right, source)))
            curr = curr.child_by_field_name("left")
        elif op_text == "->":
            left = curr.child_by_field_name("left")
            verb = _unwrap_verb(left, source)
            if verb is not None:
                segments.reverse()
                return (verb, segments)
            return None
        elif op_text == ":?":
            # Query param matcher: ``GET -> Root / "users" :? QueryParam(q)``
            curr = curr.child_by_field_name("left")
        else:
            return None

    return None


def _find_enclosing_parent_id(start_line: int, record: FileRecord) -> str | None:
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
    return None


def detect_http4s_routes(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
) -> list[Statement]:
    """Find all HttpRoutes.of / AuthedRoutes.of blocks and emit route statements for their case clauses."""
    seen_ids = {s.id for s in record.statements}
    routes: list[Statement] = []

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None and fn_node.type == "generic_function":
                fn_node = fn_node.child_by_field_name("function")

            fn_text = node_text(fn_node, source) if fn_node is not None else ""
            if fn_text.endswith(".of") and "Routes" in fn_text:
                args = node.child_by_field_name("arguments")
                if args is not None and args.type == "case_block":
                    for clause in args.named_children:
                        if clause.type == "case_clause":
                            pat = clause.child_by_field_name("pattern")
                            res = _extract_http4s_pattern(pat, source)
                            if res is not None:
                                verb, segs = res
                                endpoint = "/" + "/".join(segs) if segs else "/"
                                start = clause.start_point[0] + 1
                                end = clause.end_point[0] + 1
                                parent_id = _find_enclosing_parent_id(start, record) or record.id
                                routes.append(
                                    Statement(
                                        id=disambiguate(statement_id(path, start, clause.start_point[1]), seen_ids),
                                        parentId=parent_id,
                                        nodeType="synthetic",
                                        semanticType="route",
                                        text=node_text(clause, source),
                                        framework="http4s",
                                        handler=None,
                                        method=verb,
                                        endpoint=endpoint,
                                        routeKind="route",
                                        isRegex=False,
                                        startLine=start,
                                        endLine=end,
                                        path=path,
                                    )
                                )

        for c in node.named_children:
            walk(c)

    walk(root)
    return routes
