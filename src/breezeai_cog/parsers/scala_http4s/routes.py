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
from ..scala.statements import find_enclosing_parent_id
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
        elif op_text in (":?", "+&"):
            # Query param matchers: ``:?`` and the multi-param ``+&`` combinator.
            curr = curr.child_by_field_name("left")
        else:
            return None

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
    route_bindings: dict[str, list[Statement]] = {}
    call_bindings: dict[int, str] = {}
    string_bindings: dict[str, str] = {}

    def collect_bindings(node: Node) -> None:
        if node.type == "val_definition":
            name = node.child_by_field_name("pattern")
            value = node.child_by_field_name("value")
            if name is not None and name.type == "identifier" and value is not None:
                binding = node_text(name, source)
                if value.type == "string":
                    string_bindings[binding] = node_text(value, source).strip('"\'')
                if value.type == "call_expression":
                    call_bindings[value.start_byte] = binding
        for child in node.named_children:
            collect_bindings(child)

    collect_bindings(root)

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None and fn_node.type == "generic_function":
                fn_node = fn_node.child_by_field_name("function")

            fn_text = node_text(fn_node, source) if fn_node is not None else ""
            if fn_text.endswith(".of") and "Routes" in fn_text:
                route_binding = call_bindings.get(node.start_byte)
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
                                parent_id = find_enclosing_parent_id(start, record)
                                route = Statement(
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
                                routes.append(route)
                                if route_binding is not None:
                                    route_bindings.setdefault(route_binding, []).append(route)

        for c in node.named_children:
            walk(c)

    def resolve_prefix(node: Node | None) -> str | None:
        if node is None:
            return None
        if node.type == "string":
            return node_text(node, source).strip('"\'')
        if node.type == "identifier":
            return string_bindings.get(node_text(node, source))
        return None

    def join_paths(prefix: str | None, endpoint: str | None) -> str | None:
        if prefix is None or endpoint is None:
            return None
        left = prefix.rstrip("/")
        right = endpoint.lstrip("/")
        if not left:
            return "/" + right if right else "/"
        return left + ("/" + right if right else "")

    def apply_router_mounts(node: Node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and node_text(fn, source).rsplit(".", 1)[-1] == "Router":
                args = node.child_by_field_name("arguments")
                arrow = None
                if args is not None:
                    arrow = next(
                        (child for child in args.named_children if child.type == "infix_expression"),
                        None,
                    )
                if arrow is not None:
                    op = arrow.child_by_field_name("operator")
                    target = arrow.child_by_field_name("right")
                    if op is not None and node_text(op, source) == "->" and target is not None:
                        prefix = resolve_prefix(arrow.child_by_field_name("left"))
                        for route in route_bindings.get(node_text(target, source), []):
                            route.endpoint = join_paths(prefix, route.endpoint)
        for child in node.named_children:
            apply_router_mounts(child)

    walk(root)
    apply_router_mounts(root)
    return routes
