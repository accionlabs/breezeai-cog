"""Vert.x detection over the dekobon **Groovy** AST. Mirrors ``java_vertx/events.py`` but
for Groovy's call shape: a ``method_invocation``'s callee is the ``function`` field — a bare
``identifier`` (no receiver) or a ``field_access`` (``receiver.method``) — and route paths
are GStrings (``"${prefix}/:id"``) rendered here to ``{prefix}/:id``.

Handles both ``RouteMatcher`` idioms:

  route.get("${prefix}/x", handler)                              # receiver-qualified
  def rm = new RouteMatcher(); rm.with { get("${p}/x", h) }      # bare, inside a scope

The shared semantic decision table lives in ``parsers/vertx_common``; only the Groovy AST
extraction (``_parts`` / ``_render_path``) and the bare-call ``route_scope`` inference are
here. Mutates ``record``; returns True if anything matched."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import FactSpec, register_fact
from ..emits import Emit
from ..treesitter import node_text
from ..vertx_common import (
    vertx_emit,
    HANDLER_CALLS,
    HANDLER_SEMANTICS,
    MOUNT_SUB_ROUTER,
    MOUNTED_FACTORIES,
    FactoryMount,
    factory_mount,
    resolve_mounted_factories,
    router_name,
    SUB_ROUTER_CALL,
    SubRouterMount,
    classify_call,
    handler_info,
    join_path,
    method_lines,
    render_address,
    service_builder_vars,
    sub_router_bases,
)


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _render_path(node: Node, source: bytes, consts: dict[str, str] | None = None) -> str | None:
    """Render a Groovy ``string_literal`` (possibly a GString) to a path. An interpolated
    constant (``consts``) is substituted; any other interpolation is collapsed to a ``{name}``
    placeholder: ``"${prefix}/:platformId/empno"`` → ``{prefix}/:platformId/empno``;
    ``"${config.'path'}/:filterId"`` → ``{config.path}/:filterId``. A placeholder marks an
    unknown part — the route's endpoint is then left empty (``classify_call``)."""
    if node.type != "string_literal":
        return None
    parts: list[str] = []
    for c in node.named_children:
        if c.type == "string_fragment":
            parts.append(node_text(c, source))
        elif "interpolation" in c.type:  # gstring_brace_interpolation / gstring_interpolation
            inner = node_text(c, source).strip()
            if inner.startswith("${") and inner.endswith("}"):
                inner = inner[2:-1]
            elif inner.startswith("$"):
                inner = inner[1:]
            inner = inner.replace("'", "").replace('"', "").strip()
            known = (consts or {}).get(inner)
            parts.append(known if known is not None else "{" + inner + "}")
    if parts:
        return "".join(parts) or None
    text = node_text(node, source).strip("\"'")  # plain literal, no child fragments
    return text or None


#: Nodes whose declarations are visible to their descendants.
_SCOPES = ("block", "closure", "source_file", "class_body")
#: Nodes that take parameters; a method's body cannot see the locals around it.
_CALLABLES = ("method_declaration", "constructor_declaration", "closure")
_PARAMETERS = ("formal_parameter", "closure_parameter")


def _scope_of(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type not in _SCOPES:
        parent = parent.parent
    return parent


class _Literals:
    """Variables bound once to a plain string literal and never reassigned (``def prefix =
    '/api'``), folded only where Groovy sees them: a GString path may interpolate them.

    A name is looked up from the use outwards, as Groovy resolves it: the locals of each
    enclosing block, closure or script body — up to the enclosing method, whose body cannot see
    the locals around it — then the fields of the enclosing classes. A method or closure
    parameter of that name, or a local that is not a plain literal (``def prefix = cfg.base``),
    declared twice in one scope or reassigned, hides any outer value and any repository constant
    of that name: the part is unknown (``{prefix}``), never another scope's value. A field bound
    to an expression (``static final String A = BASE + '/x'``) is left to the constants."""

    def __init__(self, root: Node, source: bytes) -> None:
        values: dict[int, dict[str, list[str | None]]] = {}
        self._params: dict[int, set[str]] = {}
        assigned: set[str] = set()

        def walk(n: Node) -> None:
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if n.type == "variable_declarator" and name is not None and value is not None \
                    and name.type == "identifier" and (scope := _scope_of(n)) is not None:
                literal = (_render_path(value, source)
                           if value.type == "string_literal" and not any(
                               "interpolation" in c.type for c in value.named_children)
                           else None)
                if literal is not None or scope.type != "class_body":  # a field's expression
                    values.setdefault(scope.id, {}).setdefault(       # is the constants' job
                        node_text(name, source), []).append(literal)
            elif n.type in _PARAMETERS and n.parent is not None:
                owner = n.parent.parent  # (formal|closure)_parameters → method / closure
                ident = [c for c in n.named_children if c.type == "identifier"]
                if owner is not None and ident:
                    self._params.setdefault(owner.id, set()).add(node_text(ident[-1], source))
            elif n.type == "assignment_expression":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    assigned.add(node_text(left, source))
            for c in n.named_children:
                walk(c)

        walk(root)
        # Per scope: name → its value, or None when it is not a foldable literal.
        self._scopes: dict[int, dict[str, str | None]] = {
            scope: {k: (v[0] if len(v) == 1 and k not in assigned else None)
                    for k, v in names.items()}
            for scope, names in values.items()}

    def at(self, node: Node, consts: dict[str, str]) -> dict[str, str]:
        """The values ``node`` sees: the visible locals and fields over ``consts``, minus the
        names a parameter or an unfoldable local hides."""
        seen: dict[str, str | None] = {}
        in_method = False  # past a method: only class fields stay visible
        parent = node.parent
        while parent is not None:
            if parent.type in _SCOPES and (not in_method or parent.type == "class_body"):
                for name, value in self._scopes.get(parent.id, {}).items():
                    seen.setdefault(name, value)
            if parent.type in _CALLABLES:
                for name in self._params.get(parent.id, ()):
                    seen.setdefault(name, None)
                in_method = in_method or parent.type != "closure"
            parent = parent.parent
        if not seen:
            return consts
        out = {k: v for k, v in consts.items() if k not in seen}
        out.update({k: v for k, v in seen.items() if v is not None})
        return out


def _parts(
    call: Node, source: bytes, consts: dict[str, str]
) -> tuple[str, str | None, str | None, str]:
    """(method, rendered-path, raw-first-arg, receiver) for a Groovy ``method_invocation``.
    The dekobon grammar puts the callee in the ``function`` field: a bare ``identifier``
    (receiver ``""``) or a ``field_access`` (``obj.method``). The address/path is the first
    argument: a string literal renders as a (GString-aware) path; otherwise ``consts`` folds a
    ``static final String`` constant address to its value."""
    fn = call.child_by_field_name("function")
    method = obj = ""
    if fn is not None:
        if fn.type == "field_access":
            obj_node = fn.child_by_field_name("object")
            field = fn.child_by_field_name("field")
            obj = node_text(obj_node, source) if obj_node is not None else ""
            method = node_text(field, source) if field is not None else ""
        else:
            method = node_text(fn, source)
    args = call.child_by_field_name("arguments")
    path = first_arg = None
    if args is not None and args.named_children:
        first_node = args.named_children[0]
        first_arg = node_text(first_node, source)
        path = render_address(
            first_node, source, consts, lambda n, s: _render_path(n, s, consts)
        )
    return method, path, first_arg, obj


def _handler_arg(call: Node, source: bytes) -> Node | None:
    """The handler argument of a route / consumer call: the second argument when the call takes
    the address and the handler (``eb.consumer("a", { m -> … })``, 2.x ``rm.get("/x", h)``; a
    third, 2.x ``registerHandler`` completion handler, is not it), else the argument of the last
    chained ``.handler(…)``. In this grammar a chained call is
    ``method_invocation(function: field_access(object: <call>, field: handler))``."""
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    found = named[1] if len(named) >= 2 else None
    node = call
    while (access := node.parent) is not None and access.type == "field_access" \
            and access.child_by_field_name("object") == node \
            and (parent := access.parent) is not None and parent.type == "method_invocation":
        field = access.child_by_field_name("field")
        if field is not None and node_text(field, source) in HANDLER_CALLS:
            pargs = parent.child_by_field_name("arguments")
            found = pargs.named_children[0] if pargs is not None and pargs.named_children else None
        node = parent
    return found


_NOT_MOUNTED = object()


def _mount_arg(node: Node, source: bytes) -> str | None:
    """A mounted router given by name: ``apiRouter`` / ``this.apiRouter`` → ``apiRouter``."""
    if node.type == "identifier":
        return node_text(node, source)
    if node.type == "field_access":
        return router_name(node_text(node, source))
    return None


def _mount_calls(
    calls: list[Node], source: bytes, consts: dict[str, str], literals: _Literals,
) -> list[tuple[Node, Node, str, str | None, Node | None]]:
    """Every sub-router mount: (the mount call, the mounted argument, the parent router, the
    prefix, the ``route("/api/*")`` call it hangs off or None). ``router.route("/api/*")
    .subRouter(sub)`` and ``router.mountSubRouter("/api", sub)``. In this grammar a chained call
    is ``method_invocation(function: field_access(object: <call>, …))``."""
    out: list[tuple[Node, Node, str, str | None, Node | None]] = []
    for call in calls:
        method, prefix, _, obj = _parts(call, source, literals.at(call, consts))
        args = call.child_by_field_name("arguments")
        named = list(args.named_children) if args is not None else []
        if method == SUB_ROUTER_CALL and len(named) == 1:
            fn = call.child_by_field_name("function")
            inner = fn.child_by_field_name("object") if fn is not None else None
            if inner is None or inner.type != "method_invocation":
                continue
            inner_method, inner_path, inner_arg, inner_obj = _parts(
                inner, source, literals.at(inner, consts))
            if inner_method == "route" and inner_arg is not None:
                out.append((call, named[0], router_name(inner_obj), inner_path, inner))
        elif method == MOUNT_SUB_ROUTER and len(named) == 2:
            out.append((call, named[1], router_name(obj), prefix, None))
    return out


def _sub_router_mounts(
    calls: list[Node], source: bytes, consts: dict[str, str], literals: _Literals
) -> tuple[list[SubRouterMount], set[tuple[int, int]]]:
    """Sub-router mounts by name in this file (a variable or a field), and the
    ``route("/api/*")`` calls every mount hangs off (the mount's path, not routes — whatever is
    mounted). A mounted factory call is collected repo-wide instead (:func:`_factory_mounts`)."""
    mounts: list[SubRouterMount] = []
    mount_routes: set[tuple[int, int]] = set()
    for _, arg, parent, prefix, inner in _mount_calls(calls, source, consts, literals):
        if inner is not None:
            mount_routes.add((inner.start_byte, inner.end_byte))
        name = _mount_arg(arg, source)
        if name is not None:
            mounts.append((name, parent, prefix))
    return mounts, mount_routes


def _enclosing(node: Node, source: bytes) -> tuple[str | None, str | None]:
    """(class simple name, method name) a node is declared in."""
    method = cls = None
    parent = node.parent
    while parent is not None:
        if parent.type in ("method_declaration", "constructor_declaration") and method is None:
            name = parent.child_by_field_name("name")
            method = node_text(name, source) if name is not None else None
        elif parent.type in ("class_declaration", "enum_declaration"):
            name = parent.child_by_field_name("name")
            cls = node_text(name, source) if name is not None else None
            break
        parent = parent.parent
    return cls, method


def _factory_routers(root: Node, source: bytes) -> dict[tuple[str, str], str]:
    """``(class, method)`` → the router variable a method creates (``Router.router(…)``) and
    returns — with ``return`` or as its last expression: a router factory."""
    out: dict[tuple[str, str], str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "method_declaration":
            continue
        body = node.child_by_field_name("body")
        if body is None:
            continue
        created: set[str] = set()
        returned: set[str] = set()
        inner = [body]
        while inner:
            n = inner.pop()
            if n.type in ("closure", "class_body") and n is not body:
                continue  # a nested scope returns its own values
            inner.extend(n.named_children)
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if n.type in ("variable_declarator", "local_variable_declaration") \
                    and name is not None and value is not None \
                    and node_text(value, source).replace(" ", "").startswith("Router.router("):
                created.add(node_text(name, source))
            elif n.type == "return_statement":
                value = next(iter(n.named_children), None)
                if value is not None and value.type == "identifier":
                    returned.add(node_text(value, source))
        last = body.named_children[-1] if body.named_children else None
        if last is not None and last.type == "expression_statement" and last.named_children \
                and last.named_children[0].type == "identifier":  # Groovy's implicit return
            returned.add(node_text(last.named_children[0], source))
        cls, method = _enclosing(body, source)
        both = created & returned
        if cls and method and len(both) == 1:
            out[(cls, method)] = both.pop()
    return out


def _factory_key(call: Node, source: bytes) -> tuple[str, bool] | None:
    """A mounted factory call → (``Class.method``, True), or (``method``, False) when its class
    is not known from the call (``api.router()``). Not a call → None."""
    if call.type != "method_invocation":
        return None
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        cls, _ = _enclosing(call, source)
        method = node_text(fn, source)
        return (f"{cls}.{method}", True) if cls else (method, False)
    obj, field = fn.child_by_field_name("object"), fn.child_by_field_name("field")
    if field is None:
        return None
    method = node_text(field, source)
    if obj is not None and obj.type == "identifier" and node_text(obj, source)[:1].isupper():
        return f"{node_text(obj, source)}.{method}", True  # a static call
    return method, False


def _factory_mounts(root: Node, source: bytes, rel: str, repo_root: Path | None) -> list[FactoryMount] | None:
    """The factory calls this file mounts, for the ``vertx.mounted-factories`` fact. A mount on
    a router that is itself mounted here has a prefix cog does not compose here → None."""
    calls = _invocations(root)
    mounts = _mount_calls(calls, source, {}, _Literals(root, source))
    subs = {name for _, arg, _, _, _ in mounts if (name := _mount_arg(arg, source)) is not None}
    out: list[FactoryMount] = []
    for _, arg, parent, prefix, _ in mounts:
        key = _factory_key(arg, source)
        if key is not None:
            out.append((key[0], None if parent in subs else prefix, key[1]))
    return out or None


register_fact(FactSpec(
    name=MOUNTED_FACTORIES, language="groovy", gate=(b"ubRouter",),
    collect=_factory_mounts, resolve=resolve_mounted_factories,
))


def detect_vertx_groovy(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str] | None = None,
    index: object | None = None,
) -> list[Emit]:
    """This file's Vert.x routes, consumers, sends and timers, for :func:`~..emits.apply`.
    ``consts`` (``name → value``) folds symbolic addresses to their String value. ``index`` (the
    Groovy index) carries the ``vertx.mounted-factories`` fact."""
    found: list[Emit] = []
    consts = consts or {}
    literals = _Literals(root, source)
    # Vert.x 2.x RouteMatcher: bare `get("/x", h)` calls (no receiver) inside `rm.with {}`
    # are routes only when the file actually builds a RouteMatcher — a cheap file-level gate
    # that keeps a stray `map.get(...)` from being mistaken for a route.
    uses_routematcher = b"RouteMatcher" in source
    methods = method_lines(record.functions)
    calls = _invocations(root)
    mounts, mount_routes = (
        _sub_router_mounts(calls, source, consts, literals) if b"ubRouter" in source
        else ([], set())
    )
    bases = sub_router_bases(mounts)
    # Routers a method of this file creates and returns: served where their factory is mounted.
    factories = _factory_routers(root, source) if b"Router.router" in source else {}
    service_vars = service_builder_vars(source) if b"setAddress" in source else {}

    for call in calls:
        if (call.start_byte, call.end_byte) in mount_routes:  # a sub-router's mount path: wiring
            continue
        method, endpoint_path, first_arg, obj = _parts(call, source, literals.at(call, consts))
        route_scope = bool(
            uses_routematcher and obj == "" and endpoint_path and "/" in endpoint_path
        )
        info = classify_call(method, endpoint_path, first_arg, obj, route_scope=route_scope,
                             service_vars=service_vars)
        if info is None:
            continue
        semantic, verb, endpoint, route_kind = info
        unknown_base = False
        recv = router_name(obj)
        base: str | None | object = _NOT_MOUNTED
        if semantic == "route" and recv in bases:  # a sub-router's route: under its mount
            base = bases[recv]
        elif semantic == "route" and factories:
            cls, method_name = _enclosing(call, source)
            if cls and method_name and factories.get((cls, method_name)) == recv:
                mounted, prefix = factory_mount(index, cls, method_name)
                if mounted:
                    base = prefix
        if base is not _NOT_MOUNTED:
            if base is None or endpoint is None:
                unknown_base, endpoint = True, None
            else:
                endpoint = join_path(str(base), endpoint)
        handler, handler_line = (
            handler_info(_handler_arg(call, source), source, methods)
            if semantic in HANDLER_SEMANTICS else (None, None)
        )

        found.append(vertx_emit(call, semantic, verb, endpoint, route_kind, handler,
                                handler_line, unknown_base=unknown_base))

    return found
