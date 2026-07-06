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
from ..statements_common import render_concat, url_placeholder
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


def _verb_and_index(name: str, args: list[str]) -> tuple[str, int] | None:
    """Map a method decorator (name, arg-texts) -> (HTTP verb, index of the path argument),
    or None if not a route. ``@get('/x')`` -> path is arg 0; ``@operation('patch','/x')``
    -> verb is arg 0, path is arg 1."""
    verb = _METHOD_DECORATORS.get(name)
    if verb is not None:
        return verb, 0
    if name == "operation" and args:
        return _unquote(args[0]).upper(), 1
    return None


def _decorator_args(dec: Node) -> list[Node]:
    """The argument nodes of a decorator's call, e.g. the nodes inside ``@get(<here>)``."""
    inner = dec.named_children[0] if dec.named_children else None
    if inner is None or inner.type != "call_expression":
        return []
    arglist = inner.child_by_field_name("arguments")
    return list(arglist.named_children) if arglist is not None else []


def _render_literal(node: Node, source: bytes) -> str | None:
    """A path expression -> string, with interpolations/variables as ``{name}`` placeholders;
    ``None`` if not a renderable literal (caller placeholders it). Handles string literals,
    template literals, and ``+`` concatenation — so ``appConfig.apiPathV2 + '/x'`` renders as
    the well-formed ``{apiPathV2}/x`` instead of a malformed ``+``-spliced string. Route
    prefixes are kept (no leading-base stripping — the api-version segment is meaningful)."""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    if node.type == "template_string":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_fragment":
                parts.append(node_text(c, source))
            elif c.type == "template_substitution":
                expr = c.named_children[0] if c.named_children else None
                parts.append(url_placeholder(node_text(expr, source)) if expr is not None else "{param}")
        return "".join(parts)
    if node.type == "binary_expression":  # string concatenation
        return render_concat(node, source, _render_literal)
    return None


def _render_path(node: Node, source: bytes) -> str:
    r = _render_literal(node, source)
    return r if r is not None else url_placeholder(node_text(node, source))


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
                    vi = _verb_and_index(d.name, d.args)
                    if vi is None:
                        continue
                    verb, idx = vi
                    arg_nodes = _decorator_args(dec)
                    sub = _render_path(arg_nodes[idx], source) if idx < len(arg_nodes) else ""
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
