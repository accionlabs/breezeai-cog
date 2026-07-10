"""ASP.NET route detection.

Two idioms, both emitted as ``route`` statements:

* **Controllers — off the record.** The C#/VB base parser already captured
  ``[ApiController]`` / ``[Route]`` / ``[HttpGet]`` onto ``Class.decorators`` /
  ``Function.decorators`` / ``Parameter.decorators``, so :func:`detect_controller_routes`
  reads the ``FileRecord`` directly (no AST re-walk). Because it only touches the
  language-agnostic record shape, the VB ASP.NET parser reuses it verbatim.

  The full route template is composed from three places, so all three are resolved:
  the controller ``[Route]`` prefix (possibly **inherited from a base/abstract
  controller in another file**, via the repo index), the method ``[HttpGet("…")]`` verb
  attribute, and a separate method-level ``[Route("…")]``. When a controller inherits
  from a base we cannot see (an external/ambiguous base) and has no ``[Route]`` of its
  own, the prefix is unknowable — we drop the route rather than emit a fabricated
  absolute path (a wrong endpoint is worse than a missing one).
* **Minimal APIs — AST walk.** ``app.MapGet("/x", …)`` is call-based, so
  :func:`detect_minimal_api_routes` walks the tree for the ``MapGet``/``MapPost``/… calls
  (grammar-specific node names are passed in by the C#/VB caller).
"""

from __future__ import annotations

import re
from typing import Any

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Function, Statement
from ..treesitter import node_text

_HTTP_ATTRS = {
    "HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT",
    "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD", "HttpOptions": "OPTIONS",
}
_CONTROLLER_ATTRS = {"ApiController", "Controller"}
_CONTROLLER_BASES = {"Controller", "ControllerBase"}
_ROUTE_ATTRS = {"Route", "RoutePrefix"}
_AUTH_ATTRS = {"Authorize", "AllowAnonymous"}
#: framework base classes that terminate an inheritance chain cleanly — they carry no
#: user route prefix, so reaching one means the prefix is fully resolved (empty).
_FRAMEWORK_BASES = {"Controller", "ControllerBase"}
_MAP_METHODS = {
    "MapGet": "GET", "MapPost": "POST", "MapPut": "PUT",
    "MapDelete": "DELETE", "MapPatch": "PATCH",
}
# Health-check endpoint: MapHealthChecks("/path", …) → GET at the literal path.
_HEALTH_METHODS = {"MapHealthChecks"}
# GraphQL HTTP transport mount: UseGraphQL<TSchema>(path) / MapGraphQL(path) → POST. The
# path is often an identifier/omitted; graphql-dotnet's convention default is /graphql.
_GRAPHQL_MOUNTS = {"UseGraphQL", "MapGraphQL"}
_GRAPHQL_MOUNT_DEFAULT = "/graphql"
# GraphQL dev-UI middleware, each with a documented default path when called with no path.
_GRAPHQL_UIS = {
    "UseGraphQLPlayground": "/ui/playground",
    "UseGraphQLVoyager": "/ui/voyager",
    "UseGraphQLGraphiQL": "/ui/graphiql",
    "UseGraphQLAltair": "/ui/altair",
}
# MapControllers()/MapControllerRoute() only *enable* attribute/conventional routing; the
# concrete controller endpoints come from detect_controller_routes, so they emit nothing
# here (a conventional-route pattern like "{controller}/{action}" is not a callable path).


def simple_attr_name(name: str) -> str:
    """Normalize a C#/VB attribute name to its short form: an attribute may be written
    ``[HttpGet]`` or ``[HttpGetAttribute]`` (and svcutil emits the full form) — both bind to
    the same class, so drop a trailing ``Attribute``."""
    return name[: -len("Attribute")] if name.endswith("Attribute") and name != "Attribute" else name


def _first_arg(dec: Decorator) -> str:
    """First positional (non-named) attribute arg — the route template, if any."""
    for raw in dec.args:
        s = raw.strip()
        if "=" not in s or s.lstrip().startswith('"'):
            return s.strip('"')
    return ""


def _controller_token(base: str, class_name: str) -> str:
    """Expand the ``[controller]`` route token to the controller name sans suffix."""
    name = class_name[:-len("Controller")] if class_name.endswith("Controller") else class_name
    return base.replace("[controller]", name).replace("{controller}", name)


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _convention_endpoint(class_name: str, action: str) -> str:
    """ASP.NET MVC **convention** route, used when no attribute resolves the path (classic
    MVC 5 routing declares ``{controller}/{action}`` in RouteConfig, not per action):
    ``/{controller}/{action}`` with the ``Controller`` suffix dropped
    (``CatalogController.Create`` → ``/Catalog/Create``)."""
    ctrl = class_name[: -len("Controller")] if class_name.endswith("Controller") else class_name
    return f"/{ctrl}/{action}"


def _route_template(decorators: list[Decorator]) -> str | None:
    """First ``[Route]``/``[RoutePrefix]`` template among ``decorators`` (``None`` when
    absent — distinct from a present-but-empty ``[Route("")]``)."""
    for d in decorators:
        if simple_attr_name(d.name) in _ROUTE_ATTRS:
            return _first_arg(d)
    return None


def _guards(decorators: list[Decorator]) -> list[str]:
    return [n for d in decorators if (n := simple_attr_name(d.name)) in _AUTH_ATTRS]


def _resolve_chain(cls: Any, index: Any) -> tuple[str | None, list[str], bool]:
    """Walk ``cls``'s inheritance chain through the repo index, composing the
    controller-level route template (nearest-defined wins) and unioning the auth guards.

    Returns ``(route_template, guards, resolved)``. ``resolved`` is ``False`` when the
    chain ends at a base we cannot see (declared outside the repo, or an ambiguous name):
    a prefix declared on that base would be invisible, so the caller treats a missing
    route as unknown rather than empty (honest-null)."""
    heritage_map = getattr(index, "class_heritage", None) or {}
    route = _route_template(cls.decorators)
    guards = _guards(cls.decorators)
    base = cls.extends
    seen = {cls.name}
    resolved = True
    while base is not None:
        short = base.rsplit(".", 1)[-1].split("<", 1)[0]
        if short in seen:  # inheritance cycle (shouldn't happen in valid C#) — stop
            break
        seen.add(short)
        heritage = heritage_map.get(short, "missing")
        if heritage == "missing":  # base not declared in the repo
            resolved = short in _FRAMEWORK_BASES  # framework root = clean; unknown = incomplete
            break
        if heritage is None:  # ambiguous name — do not resolve through it
            resolved = False
            break
        if route is None:
            route = _route_template(heritage.decorators)
        guards.extend(g for g in _guards(heritage.decorators) if g not in guards)
        base = heritage.extends
    return route, guards, resolved


def _method_templates(http_tmpl: str, method_route: str | None) -> list[str]:
    """The method-level route segment(s). Normally one — the ``[HttpX("…")]`` arg, or a
    sibling ``[Route("…")]`` when the verb attribute carries none (a standard split idiom).
    When both independently carry a *different* template, ASP.NET registers both routes."""
    if http_tmpl and method_route is not None and http_tmpl != method_route:
        return [http_tmpl, method_route]
    if http_tmpl:
        return [http_tmpl]
    if method_route is not None:
        return [method_route]
    return [""]


def _response_dto(return_type: str | None) -> str | None:
    """Unwrap ``Task<…>`` / ``ActionResult<…>`` / ``Task(Of …)`` to the payload type;
    bare action results (``IActionResult``/``ActionResult``/``void``) → None."""
    if not return_type:
        return None
    t = return_type.strip()
    # C# generic: Foo<Bar> → Bar
    while "<" in t and t.endswith(">"):
        t = t[t.index("<") + 1: -1].strip()
    # VB generic: Foo(Of Bar) → Bar
    while t.startswith(("Task(Of ", "ValueTask(Of ", "ActionResult(Of ")) and t.endswith(")"):
        t = t[t.index("(Of ") + 4: -1].strip()
    if t in ("IActionResult", "ActionResult", "void", "Void", "Task", "ValueTask", ""):
        return None
    return t


def _request_dto(fn: Function) -> str | None:
    for p in fn.params:
        if any(simple_attr_name(d.name) == "FromBody" for d in p.decorators):
            return p.type or None
    return None


def _is_controller(cls: Any) -> bool:
    if {simple_attr_name(d.name) for d in cls.decorators} & _CONTROLLER_ATTRS:
        return True
    ext = (cls.extends or "").rsplit(".", 1)[-1]
    return ext in _CONTROLLER_BASES or ext.endswith(("Controller", "ControllerBase"))


def detect_controller_routes(record: FileRecord, index: Any = None) -> list[Statement]:
    # (controller-route prefix, chain guards, chain-resolved, has-any-[Route], class name) per id
    controllers: dict[str, tuple[str, list[str], bool, bool, str]] = {}
    for cls in record.classes:
        if not _is_controller(cls):
            continue
        route, guards, resolved = _resolve_chain(cls, index)
        base = _controller_token(route, cls.name) if route is not None else ""
        controllers[cls.id] = (base, guards, resolved, route is not None, cls.name)
    if not controllers:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        info = controllers.get(fn.parentId)
        if info is None:
            continue
        base, cls_guards, resolved, has_class_route, cls_name = info
        # honest-null: an inherited base we cannot see may carry the route prefix — without
        # it, and with no [Route] of our own, the absolute path is unknowable. Emit nothing
        # rather than a fabricated endpoint (a wrong route is worse than a missing one).
        if not has_class_route and not resolved:
            continue
        method_route = _route_template(fn.decorators)
        fn_guards = _guards(fn.decorators)
        auth_required = ("Authorize" in cls_guards or "Authorize" in fn_guards) or None
        all_guards = (cls_guards + fn_guards) or None
        for dec in fn.decorators:
            verb = _HTTP_ATTRS.get(simple_attr_name(dec.name))
            if verb is None:  # method template composed from [HttpX] + sibling [Route] below
                continue
            for sub in _method_templates(_first_arg(dec), method_route):
                # attribute route when one resolves; else MVC convention /{controller}/{action}
                endpoint = _join(base, sub) if (base or sub) else _convention_endpoint(cls_name, fn.name)
                routes.append(Statement(
                    id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
                    parentId=fn.id,
                    nodeType="synthetic",
                    semanticType="route",
                    text=f"[{dec.name}]",
                    method=verb,
                    endpoint=endpoint,
                    framework="aspnet",
                    handler=fn.name,
                    handlerLine=fn.startLine,
                    routeKind="route",
                    isRegex=False,
                    authRequired=auth_required,
                    guards=all_guards,
                    requestDTO=_request_dto(fn),
                    responseDTO=_response_dto(fn.returnType),
                    startLine=fn.startLine,
                    endLine=fn.endLine,
                    path=fn.path,
                ))
    return routes


def detect_minimal_api_routes(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
    *,
    invocation_type: str,
    member_type: str,
) -> list[Statement]:
    """AST-walk for endpoint registrations on an ``app``/``endpoints`` object:
    ``MapGet/MapPost/…`` minimal APIs, ``MapHealthChecks``, and the GraphQL HTTP mount +
    dev-UI middleware (``UseGraphQL``/``MapGraphQL``/``UseGraphQLPlayground``/…). Grammar-
    specific node/field names are supplied by the C#/VB caller."""
    routes: list[Statement] = []
    name_field = "name" if member_type == "member_access_expression" else "member"
    fn_field = "function" if invocation_type == "invocation_expression" else "target"

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == invocation_type:
                func = child.child_by_field_name(fn_field)
                if func is not None and func.type == member_type:
                    method = _member_method_name(func, source, name_field)
                    spec = _endpoint_spec(method, child, root, source)
                    if spec is not None:
                        verb, framework, endpoint = spec
                        start = child.start_point[0] + 1
                        routes.append(Statement(
                            id=disambiguate(statement_id(path, start, child.start_point[1]), seen_ids),
                            parentId=path,
                            nodeType=child.type,
                            semanticType="route",
                            text=node_text(child, source).split("\n", 1)[0][:200],
                            method=verb,
                            endpoint=endpoint,
                            framework=framework,
                            routeKind="route",
                            isRegex=False,
                            startLine=start,
                            endLine=child.end_point[0] + 1,
                            path=path,
                        ))
            visit(child)

    visit(root)
    return routes


def _member_method_name(func: Node, source: bytes, name_field: str) -> str:
    """Bare method name of a member call, dropping generic args so ``UseGraphQL<ISchema>``
    keys as ``UseGraphQL`` (the name node is a ``generic_name`` in that case)."""
    name_node = func.child_by_field_name(name_field)
    if name_node is None:
        return ""
    if name_node.type == "generic_name":
        ident = next((c for c in name_node.named_children if c.type == "identifier"), None)
        return node_text(ident, source) if ident is not None else ""
    return node_text(name_node, source)


def _endpoint_spec(
    method: str, call: Node, root: Node, source: bytes
) -> tuple[str, str, str] | None:
    """``(verb, framework, endpoint)`` for a recognized registration call, or None to skip.
    Returns None (rather than a guessed path) when the endpoint cannot be determined —
    absent beats wrong."""
    if method in _MAP_METHODS:
        return _MAP_METHODS[method], "aspnet", _minimal_path(call, source) or "/"
    if method in _HEALTH_METHODS:
        endpoint = _minimal_path(call, source)
        return ("GET", "aspnet", endpoint) if endpoint is not None else None
    if method in _GRAPHQL_MOUNTS:
        return "POST", "graphql", _graphql_mount_path(call, root, source)
    if method in _GRAPHQL_UIS:
        endpoint = _ui_path(call, source, _GRAPHQL_UIS[method])
        return ("GET", "graphql", endpoint) if endpoint is not None else None
    return None


def _positional_args(call: Node) -> list[Node]:
    """Inner expression node of each positional ``argument`` in a call (generic type args
    live on the function, not here)."""
    args = call.child_by_field_name("arguments")
    out = []
    for arg in (args.named_children if args is not None else []):
        inner = arg.named_children[0] if arg.type == "argument" and arg.named_children else arg
        if inner is not None:
            out.append(inner)
    return out


def _graphql_mount_path(call: Node, root: Node, source: bytes) -> str:
    """Endpoint of a GraphQL HTTP mount: a literal path, an in-file resolved string
    identifier, else graphql-dotnet's ``/graphql`` convention (also used when no path arg)."""
    args = _positional_args(call)
    if not args:
        return _GRAPHQL_MOUNT_DEFAULT
    first = args[0]
    if first.type == "string_literal":
        return _literal(first, source)
    if first.type == "identifier":
        return _resolve_str(node_text(first, source), root, source) or _GRAPHQL_MOUNT_DEFAULT
    return _GRAPHQL_MOUNT_DEFAULT


def _ui_path(call: Node, source: bytes, default: str) -> str | None:
    """Endpoint of a GraphQL dev-UI middleware: a literal path arg, else the library default
    when called with no args. An unparseable options object → None (path may be overridden;
    do not guess)."""
    args = _positional_args(call)
    if not args:
        return default
    if args[0].type == "string_literal":
        return _literal(args[0], source)
    return None


def _resolve_str(name: str, root: Node, source: bytes) -> str | None:
    """In-file value of a string field/local (``private readonly string _x = "…";`` or
    ``var x = "…";``) — the first ``variable_declarator`` binding ``name`` to a string."""
    found: str | None = None

    def walk(n: Node) -> None:
        nonlocal found
        if found is not None:
            return
        if n.type == "variable_declarator":
            nm = n.child_by_field_name("name") or (n.named_children[0] if n.named_children else None)
            val = next((c for c in n.named_children if c.type == "string_literal"), None)
            if nm is not None and val is not None and node_text(nm, source) == name:
                found = _literal(val, source)
                return
        for c in n.named_children:
            walk(c)

    walk(root)
    return found


def _minimal_path(call: Node, source: bytes) -> str | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        lit = _find_string(arg)
        if lit is not None:
            return _literal(lit, source)
    return None


def _literal(lit: Node, source: bytes) -> str:
    """The text of a C# string literal, stripping quotes (and a ``@`` verbatim prefix)."""
    content = next((c for c in lit.named_children if c.type == "string_literal_content"), None)
    if content is not None:
        return node_text(content, source)
    return node_text(lit, source).lstrip("@").strip('"')


def _find_string(node: Node) -> Node | None:
    if node.type == "string_literal":
        return node
    for c in node.named_children:
        found = _find_string(c)
        if found is not None:
            return found
    return None


_MAP_ROUTE_METHODS = {"MapRoute", "MapHttpRoute"}
_CTRL_RE = re.compile(r'controller\s*=\s*"([^"]+)"')
_ACTION_RE = re.compile(r'action\s*=\s*"([^"]+)"')


def _maproute_url(call: Node, source: bytes) -> str | None:
    """The URL-template arg of a ``MapRoute`` call — the string literal that looks like a
    route template (contains ``{`` or ``/``), skipping the leading route-name arg."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    strings = [node_text(s, source).strip('"') for arg in args.named_children
               if (s := _find_string(arg)) is not None]
    for s in strings:
        if "{" in s or "/" in s:
            return s
    return strings[1] if len(strings) >= 2 else None


def detect_route_registrations(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
    *,
    invocation_type: str,
    member_type: str,
) -> list[Statement]:
    """AST-walk for MVC / Web-API **convention-route registrations** —
    ``routes.MapRoute(name, url, defaults)`` / ``config.Routes.MapHttpRoute(…)`` declared in
    ``RouteConfig`` / ``Global.asax`` / an ``AreaRegistration``. Emits the declared URL
    template as a ``route`` (method ``ANY``, since a convention route matches any verb), with
    the default ``controller``[.``action``] as the handler. Grammar-specific node/field names
    are supplied by the C#/VB caller (Phase 2 of the MVC endpoint-resolution work)."""
    routes: list[Statement] = []
    name_field = "name" if member_type == "member_access_expression" else "member"
    fn_field = "function" if invocation_type == "invocation_expression" else "target"

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == invocation_type:
                func = child.child_by_field_name(fn_field)
                if func is not None and func.type == member_type:
                    name_node = func.child_by_field_name(name_field)
                    method = node_text(name_node, source) if name_node is not None else ""
                    url = _maproute_url(child, source) if method in _MAP_ROUTE_METHODS else None
                    if url:
                        text = node_text(child, source)
                        ctrl, action = _CTRL_RE.search(text), _ACTION_RE.search(text)
                        handler = (f"{ctrl.group(1)}.{action.group(1)}" if ctrl and action
                                   else ctrl.group(1) if ctrl else None)
                        start = child.start_point[0] + 1
                        routes.append(Statement(
                            id=disambiguate(statement_id(path, start, child.start_point[1]), seen_ids),
                            parentId=path,
                            nodeType=child.type,
                            semanticType="route",
                            text=text.split("\n", 1)[0][:200],
                            method="ANY",
                            endpoint="/" + url.strip("/"),
                            framework="aspnet",
                            handler=handler,
                            routeKind="route",
                            isRegex=False,
                            startLine=start,
                            endLine=child.end_point[0] + 1,
                            path=path,
                        ))
            visit(child)

    visit(root)
    return routes
