"""Akka HTTP directive-DSL route detection.

Akka HTTP routes are built using nested directive call expressions:
- Path directives (``path``, ``pathPrefix``, ``pathSuffix``, ``rawPathPrefix``) accumulate
  URL path segments from string literals and path matchers (``Segment``, ``IntNumber``, etc.).
- Verb directives (``get``, ``post``, ``put``, ``delete``, ``patch``, ``head``, ``options``)
  provide the HTTP method.
- Directive blocks with no HTTP method directive emit routes with ``method=None`` (honest null).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..scala.statements import find_enclosing_parent_id
from ..statements_common import url_placeholder
from ..treesitter import node_text

_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
_PATH_DIRECTIVES = frozenset({"path", "pathprefix", "pathsuffix", "rawpathprefix", "rawpath"})
_HTTP_METHOD_NAMES = frozenset(name.upper() for name in _HTTP_VERBS)


def _extract_segments(node: Node | None, source: bytes) -> list[str]:
    """Extract path segments from a directive argument (string, identifier, or infix / chain)."""
    if node is None:
        return []
    if node.type == "string":
        s = node_text(node, source).strip('"\'').strip("/")
        return [p for p in s.split("/") if p] if s else []
    if node.type == "infix_expression":
        op = node.child_by_field_name("operator")
        if op is not None and node_text(op, source) == "/":
            left = _extract_segments(node.child_by_field_name("left"), source)
            right = _extract_segments(node.child_by_field_name("right"), source)
            return left + right
    if node.type == "identifier":
        name = node_text(node, source)
        return [url_placeholder(name)]
    if node.type in ("arguments", "tuple_expression"):
        res: list[str] = []
        for c in node.named_children:
            res.extend(_extract_segments(c, source))
        return res
    return [url_placeholder(node_text(node, source))]


def _extract_lambda_params(node: Node | None, source: bytes) -> list[str]:
    """Extract parameter names from a lambda: ``{ userId => ... }`` or ``{ (a, b) => ... }``."""
    if node is None or node.type != "lambda_expression":
        return []
    params: list[str] = []
    for c in node.children:
        if c.type == "=>":
            break
        if c.type == "identifier":
            params.append(node_text(c, source))
        elif c.type in ("parameters", "bindings", "tuple_expression"):
            for p in c.named_children:
                if p.type in ("identifier", "binding", "parameter"):
                    p_name = p.child_by_field_name("name") or p
                    params.append(node_text(p_name, source))
    return params


def _apply_params_to_segments(segments: list[str], params: list[str]) -> list[str]:
    """Substitute generic placeholder segments (like ``{Segment}``) with extracted lambda parameter names."""
    if not params:
        return segments
    res = list(segments)
    p_idx = 0
    for i, seg in enumerate(res):
        if seg.startswith("{") and seg.endswith("}") and p_idx < len(params):
            res[i] = "{" + params[p_idx] + "}"
            p_idx += 1
    return res


def _explicit_method(node: Node, source: bytes) -> str | None:
    """Extract ``GET`` from an explicit ``method(HttpMethods.GET)`` directive."""
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    first = next(iter(args.named_children), None)
    if first is None:
        return None
    name = node_text(first, source).rsplit(".", 1)[-1].upper()
    return name if name in _HTTP_METHOD_NAMES else None


def detect_akkahttp_routes(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
) -> list[Statement]:
    """Walk tree-sitter AST and detect akka-http route definitions."""
    seen_ids = {s.id for s in record.statements}
    routes: list[Statement] = []

    def emit_route(node: Node, method: str | None, endpoint: str) -> None:
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        parent_id = find_enclosing_parent_id(start, record)
        routes.append(
            Statement(
                id=disambiguate(statement_id(path, start, node.start_point[1]), seen_ids),
                parentId=parent_id,
                nodeType="synthetic",
                semanticType="route",
                text=node_text(node, source),
                framework="akka-http",
                handler=None,
                method=method,
                endpoint=endpoint,
                routeKind="route",
                isRegex=False,
                startLine=start,
                endLine=end,
                path=path,
            )
        )

    def walk_directives(
        node: Node, current_path: list[str], in_route_context: bool = False
    ) -> bool:
        """Walk AST to find path and verb directives. Returns True if any route was emitted in this subtree."""
        found_any = False

        if node.type == "call_expression":
            fn_child = node.child_by_field_name("function")
            args_child = node.child_by_field_name("arguments")

            # Case 1: path("...") { ... } / pathPrefix("...") { ... }
            if fn_child is not None and fn_child.type == "call_expression":
                inner_fn = fn_child.child_by_field_name("function")
                if inner_fn is not None and inner_fn.type == "identifier":
                    dir_name = node_text(inner_fn, source).lower()
                    if dir_name in _PATH_DIRECTIVES:
                        path_args = fn_child.child_by_field_name("arguments")
                        segs = _extract_segments(path_args, source)

                        # Check for lambda parameter names in block
                        if args_child is not None:
                            lambda_node = None
                            if args_child.type == "lambda_expression":
                                lambda_node = args_child
                            elif args_child.type == "block":
                                for c in args_child.named_children:
                                    if c.type == "lambda_expression":
                                        lambda_node = c
                                        break
                            if lambda_node is not None:
                                params = _extract_lambda_params(lambda_node, source)
                                segs = _apply_params_to_segments(segs, params)

                        new_path = current_path + segs
                        sub_emitted = False
                        if args_child is not None:
                            sub_emitted = walk_block_or_expr(args_child, new_path, True)

                        if not sub_emitted:
                            # Path block with no nested HTTP verb / sub-route: emit with method=None
                            ep = "/" + "/".join(new_path) if new_path else "/"
                            emit_route(node, None, ep)
                        return True

            # Case 2: explicit method directive: method(HttpMethods.GET) { ... }
            if fn_child is not None and fn_child.type == "call_expression":
                inner_fn = fn_child.child_by_field_name("function")
                if (
                    inner_fn is not None
                    and inner_fn.type == "identifier"
                    and node_text(inner_fn, source).lower() == "method"
                    and in_route_context
                ):
                    method = _explicit_method(fn_child, source)
                    if method is not None:
                        ep = "/" + "/".join(current_path) if current_path else "/"
                        emit_route(node, method, ep)
                        return True

            # Case 3: HTTP verb directive: get { ... } / post { ... }
            # A bare verb is ambiguous with an ordinary function. Only accept it
            # after a path/route directive has established route context.
            if fn_child is not None and fn_child.type == "identifier" and in_route_context:
                fn_name = node_text(fn_child, source).lower()
                if fn_name in _HTTP_VERBS:
                    verb = fn_name.upper()
                    ep = "/" + "/".join(current_path) if current_path else "/"
                    emit_route(node, verb, ep)
                    return True

        for c in node.named_children:
            if walk_directives(c, current_path, in_route_context):
                found_any = True
        return found_any

    def walk_block_or_expr(
        node: Node, current_path: list[str], in_route_context: bool
    ) -> bool:
        found_any = False
        if node.type in ("block", "lambda_expression", "indented_block"):
            for c in node.named_children:
                if walk_directives(c, current_path, in_route_context):
                    found_any = True
        else:
            if walk_directives(node, current_path, in_route_context):
                found_any = True
        return found_any

    walk_directives(root, [])
    return routes
