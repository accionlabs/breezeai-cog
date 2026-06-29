"""FastAPI route detection — finds ``@app.get("/x")`` / ``@router.post(...)`` style
decorators and emits ``semanticType="route"`` statements parented to their handler
function (via the shared id convention, so the parentId matches the function id the
Python extraction assigned).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text

# HTTP verbs (+ websocket) exposed as decorator methods on FastAPI app/router objects.
_VERBS = {"get", "post", "put", "patch", "delete", "options", "head", "trace", "websocket"}


def _name(node: Node, source: bytes) -> str:
    n = node.child_by_field_name("name")
    return node_text(n, source) if n is not None else ""


def _endpoint(call: Node, source: bytes) -> str | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        if arg.type == "string":
            content = next((c for c in arg.named_children if c.type == "string_content"), None)
            return node_text(content, source) if content is not None else None
    return None


def _route(dec: Node, fdef: Node, source: bytes, path: str, class_name: str | None,
           seen_ids: set[str]) -> Statement | None:
    call = dec.named_children[0] if dec.named_children else None
    if call is None or call.type != "call":
        return None
    func = call.child_by_field_name("function")
    if func is None or func.type != "attribute":  # need X.verb(...)
        return None
    verb_node = func.named_children[-1] if func.named_children else None
    if verb_node is None:
        return None
    verb = node_text(verb_node, source).lower()
    if verb not in _VERBS:
        return None

    name = _name(fdef, source)
    handler_line = fdef.start_point[0] + 1
    start_line, start_col = dec.start_point[0] + 1, dec.start_point[1]
    return Statement(
        id=disambiguate(statement_id(path, start_line, start_col), seen_ids),
        parentId=function_id(path, name, handler_line, class_name=class_name),
        nodeType="decorator",
        semanticType="route",
        text=node_text(dec, source).split("\n", 1)[0],
        method=verb.upper(),
        endpoint=_endpoint(call, source),
        framework="fastapi",
        handler=name,
        handlerLine=handler_line,
        routeKind="ws" if verb == "websocket" else "route",
        startLine=start_line,
        endLine=dec.end_point[0] + 1,
        path=path,
    )


def detect_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str]) -> list[Statement]:
    """Walk the tree (tracking the enclosing class) and emit route statements."""
    routes: list[Statement] = []

    def walk(node: Node, class_name: str | None) -> None:
        for child in node.named_children:
            if child.type == "class_definition":
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, _name(child, source))
            elif child.type == "decorated_definition":
                fdef = next((c for c in child.named_children if c.type == "function_definition"), None)
                cdef = next((c for c in child.named_children if c.type == "class_definition"), None)
                if cdef is not None:
                    body = cdef.child_by_field_name("body")
                    if body is not None:
                        walk(body, _name(cdef, source))
                elif fdef is not None:
                    for dec in (c for c in child.named_children if c.type == "decorator"):
                        route = _route(dec, fdef, source, path, class_name, seen_ids)
                        if route is not None:
                            routes.append(route)
                    body = fdef.child_by_field_name("body")
                    if body is not None:
                        walk(body, class_name)
            elif child.type == "function_definition":
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, class_name)
            else:
                walk(child, class_name)

    walk(root, None)
    return routes
