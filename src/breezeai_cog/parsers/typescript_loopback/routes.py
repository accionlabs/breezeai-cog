"""LoopBack 4 route detection: controller methods carry the full route in their
decorator — ``@get('/products/{id}')`` / ``@post('/products')`` / ``@del(...)`` etc.,
or the generic ``@operation(verb, path)``. Unlike NestJS there is no required
class-level base path; the optional ``@api({basePath: '/x'})`` class decorator is
joined onto each method path when present. Emits ``semanticType="route"`` statements
parented to their handler method (via the shared id convention, so parentId matches the
base TypeScript parser's function id)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import function_id
from ...parsers.typescript.decorators import decorator
from ...parsers.typescript.functions import extract_params, payload_type, return_dto
from ..emits import Emit
from ..treesitter import node_text
from ..typescript.base_paths import app_base_path
from .facts import loopback_base_paths

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


def _served(base: str, sub: str | None, found: bool, app_base: str | None) -> str | None:
    """Controller ``base`` + route ``sub``, under the application's ``basePath`` when one was
    seen: a literal is joined in front; set-but-unresolved (``app_base`` None) → unknown."""
    if sub is None or (found and app_base is None):
        return None
    local = _join(base, sub)
    return _join(app_base, local) if found and app_base else local


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
    """A path expression -> its literal string, or ``None`` when any part is not a literal.
    Handles string literals, template literals without ``${…}``, and ``+`` concatenation of
    those (``'/api' + '/x'``). A variable part (``appConfig.apiPath + '/x'``, ``${version}``) is
    unknown: the whole path is ``None`` — never a ``{name}`` placeholder, which would read as a
    LoopBack path parameter. Route prefixes are kept (no leading-base stripping)."""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        return "".join(node_text(c, source) for c in node.named_children
                       if c.type == "string_fragment")
    if node.type == "binary_expression":  # string concatenation
        op = node.child_by_field_name("operator")
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if op is None or node_text(op, source) != "+" or left is None or right is None:
            return None
        lv, rv = _render_literal(left, source), _render_literal(right, source)
        return lv + rv if lv is not None and rv is not None else None
    return None


def _auth(decs: list[Node], source: bytes) -> tuple[bool, list[str]]:
    """LoopBack auth via ``@authenticate('jwt')`` (class- or method-level). Returns
    (any @authenticate present, strategy names). ``@authenticate.skip()`` parses as name
    ``skip`` and is correctly ignored; the object form ``@authenticate({strategy})`` counts
    as present but contributes no clean guard name."""
    present = False
    names: list[str] = []
    for dec in decs:
        d = decorator(dec, source)
        if d.name == "authenticate":
            present = True
            names.extend(_unquote(a) for a in d.args if a.strip() and not a.strip().startswith("{"))
    return present, names


def _request_dto(member: Node, source: bytes) -> str | None:
    """Declared type of the ``@requestBody()``-decorated parameter → requestDTO, as written."""
    for p in extract_params(member.child_by_field_name("parameters"), source):
        if any(d.name == "requestBody" for d in p.decorators):
            return payload_type(p.type)
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


def detect_loopback_routes(
    root: Node, source: bytes, path: str, *, index: object | None = None
) -> list[Emit]:
    """This file's controller routes, for :func:`~..emits.apply`. ``index`` is the repo's
    ``TsAliasIndex``: its ``loopback.base-paths`` fact gives the application's ``rest.basePath``,
    joined in front of every controller route of that app (unknown when it cannot be
    resolved)."""
    routes: list[Emit] = []
    found, app_base = app_base_path(loopback_base_paths(index), path)
    for cls, decs in _class_with_decorators(root):
        base = _api_base_path(decs, source)
        ctrl_auth, ctrl_guards = _auth(decs, source)  # class-level @authenticate applies to all methods
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
                m_auth, m_guards = _auth(pending, source)  # method-level @authenticate
                guards = ctrl_guards + m_guards
                auth_required = ctrl_auth or m_auth
                for dec in pending:
                    d = decorator(dec, source)
                    vi = _verb_and_index(d.name, d.args)
                    if vi is None:
                        continue
                    verb, idx = vi
                    arg_nodes = _decorator_args(dec)
                    sub: str | None = (
                        _render_literal(arg_nodes[idx], source) if idx < len(arg_nodes) else ""
                    )
                    routes.append(Emit(dec, dict(
                        semanticType="route",
                        method=verb,
                        endpoint=_served(base, sub, found, app_base),
                        framework="loopback",
                        handler=mname,
                        handlerLine=mline,
                        routeKind="route",
                        guards=guards or None,
                        authRequired=auth_required,
                        requestDTO=_request_dto(member, source),
                        responseDTO=return_dto(member, source),
                    ), parent=function_id(path, mname, mline, class_name=class_name),
                        node_type="synthetic"))
            pending = []
    return routes
