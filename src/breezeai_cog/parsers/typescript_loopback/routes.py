"""LoopBack 4 route detection: controller methods carry the full route in their
decorator — ``@get('/products/{id}')`` / ``@post('/products')`` / ``@del(...)`` etc.,
or the generic ``@operation(verb, path)``. Unlike NestJS there is no required
class-level base path; the optional ``@api({basePath: '/x'})`` class decorator is
joined onto each method path when present. Emits ``semanticType="route"`` statements
parented to their handler method (via the shared id convention, so parentId matches the
base TypeScript parser's function id)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...parsers.typescript.functions import decorator
from ...schemas import Statement
from ..treesitter import node_text

# LoopBack method decorators -> HTTP verb. ``del`` (not ``delete``) is LoopBack's
# DELETE decorator because ``delete`` is a reserved word.
_METHOD_DECORATORS = {
    "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
    "del": "DELETE", "head": "HEAD", "options": "OPTIONS",
}


def _unquote(text: str) -> str:
    return text.strip().strip("'\"`")


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _api_base_path(decorators: list[Node], source: bytes) -> str:
    """Extract ``basePath`` from a class-level ``@api({basePath: '/x'})`` decorator."""
    for dec in decorators:
        d = decorator(dec, source)
        if d.name != "api":
            continue
        inner = dec.named_children[0] if dec.named_children else None
        if inner is None or inner.type != "call_expression":
            continue
        arglist = inner.child_by_field_name("arguments")
        if arglist is None:
            continue
        for arg in arglist.named_children:
            if arg.type != "object":
                continue
            for pair in arg.named_children:
                if pair.type != "pair":
                    continue
                key = pair.child_by_field_name("key")
                value = pair.child_by_field_name("value")
                if key is not None and value is not None and _unquote(node_text(key, source)) == "basePath":
                    return _unquote(node_text(value, source))
    return ""


def _route_for(name: str, args: list[str]) -> tuple[str, str] | None:
    """Map a method decorator (name, args) -> (verb, path), or None if not a route."""
    verb = _METHOD_DECORATORS.get(name)
    if verb is not None:
        return verb, _unquote(args[0]) if args else ""
    if name == "operation" and args:
        path = _unquote(args[1]) if len(args) > 1 else ""
        return _unquote(args[0]).upper(), path
    return None


def _class_with_decorators(root: Node):
    """Yield (class_declaration, class_decorator_nodes) for top-level classes."""
    pending: list[Node] = []
    for child in root.named_children:
        if child.type == "decorator":
            pending.append(child)
            continue
        decs, cls = list(pending), None
        pending = []
        if child.type == "export_statement":
            decs += [c for c in child.named_children if c.type == "decorator"]
            cls = next((c for c in child.named_children if c.type == "class_declaration"), None)
        elif child.type == "class_declaration":
            cls = child
        if cls is not None:
            yield cls, decs


def detect_loopback_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str]) -> list[Statement]:
    routes: list[Statement] = []
    for cls, decs in _class_with_decorators(root):
        base = _api_base_path(decs, source)
        name_node = cls.child_by_field_name("name")
        class_name = node_text(name_node, source) if name_node is not None else None
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        pending: list[Node] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type == "comment":
                continue  # a comment between a route decorator and its handler must not drop it
            if member.type == "method_definition":
                mname = node_text(member.child_by_field_name("name"), source)
                mline = member.start_point[0] + 1
                for dec in pending:
                    d = decorator(dec, source)
                    route = _route_for(d.name, d.args)
                    if route is None:
                        continue
                    verb, sub = route
                    sl, sc = dec.start_point[0] + 1, dec.start_point[1]
                    routes.append(Statement(
                        id=disambiguate(statement_id(path, sl, sc), seen_ids),
                        parentId=function_id(path, mname, mline, class_name=class_name),
                        nodeType="decorator",
                        semanticType="route",
                        text=node_text(dec, source).split("\n", 1)[0],
                        method=verb,
                        endpoint=_join(base, sub),
                        framework="loopback",
                        handler=mname,
                        handlerLine=mline,
                        routeKind="route",
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    ))
            pending = []
    return routes
