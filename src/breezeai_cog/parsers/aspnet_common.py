"""ASP.NET route detection, shared by the ``csharp_aspnet`` and ``vb_aspnet`` parsers.

Two idioms, both emitted as ``route`` statements:

* **Controllers — off the record.** The C#/VB base parser already captured
  ``[ApiController]`` / ``[Route]`` / ``[HttpGet]`` onto ``Class.decorators`` /
  ``Function.decorators`` / ``Parameter.decorators``, so :func:`detect_controller_routes`
  reads the ``FileRecord`` directly (no AST re-walk). Because it only touches the
  language-agnostic record shape, C# and VB use it verbatim.

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

from ..emit import disambiguate, statement_id
from ..schemas import Decorator, FileRecord, Function, Statement
from .dotnet_common import response_dto, simple_attr_name
from .index_common import walk_heritage
from .treesitter import node_text

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
# The GraphQL HTTP transport mount — UseGraphQL<TSchema>(path) / MapGraphQL(path) — serves every
# operation of the schema: wiring (contract §4), so it has no record; the operations (resolvers)
# are the bindings.
GRAPHQL_TRANSPORT_RE = re.compile(rb"\.(?:UseGraphQL|MapGraphQL)\s*[<(]")
# GraphQL dev-UI middleware, each with a documented default path when called with no path.
_GRAPHQL_UIS = {
    "UseGraphQLPlayground": "/ui/playground",
    "UseGraphQLVoyager": "/ui/voyager",
    "UseGraphQLGraphiQL": "/ui/graphiql",
    "UseGraphQLAltair": "/ui/altair",
}
#: Calls that register a URL served by framework code, with no handler of ours: its binding is
#: recorded with an empty handler (contract §4).
FRAMEWORK_SERVED: frozenset[str] = frozenset(_HEALTH_METHODS | _GRAPHQL_UIS.keys())
# MapControllers()/MapControllerRoute() only *enable* attribute/conventional routing; the
# concrete controller endpoints come from detect_controller_routes, so they emit nothing
# here (a conventional-route pattern like "{controller}/{action}" is not a callable path).


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
    """Compose the controller-level route template (nearest-defined wins) and union the auth
    guards across ``cls``'s inheritance chain (walked via :func:`walk_heritage`).

    Returns ``(route_template, guards, resolved)``. ``resolved`` is ``False`` when the chain
    ends at a base we cannot see (outside the repo and not a known framework root, or an
    ambiguous name): a prefix declared there would be invisible, so the caller treats a
    missing route as unknown rather than empty (honest-null)."""
    heritage_map = getattr(index, "class_heritage", None) or {}
    chain = walk_heritage(cls.name, cls.extends, heritage_map)
    route = _route_template(cls.decorators)
    guards = _guards(cls.decorators)
    for heritage in chain.ancestors:
        if route is None:
            route = _route_template(heritage.decorators)
        guards.extend(g for g in _guards(heritage.decorators) if g not in guards)
    if chain.unresolved is None:
        resolved = True  # chain ended in-repo (or a cycle)
    elif chain.ambiguous:
        resolved = False
    else:  # base outside the repo — clean only if it's a known framework root
        resolved = chain.unresolved in _FRAMEWORK_BASES
    return route, guards, resolved


def _is_absolute(sub: str) -> bool:
    """A method route template is absolute — it overrides the controller prefix rather than
    combining with it — when it starts with ``/`` (app-root) or ``~/`` (the explicit
    ignore-controller-route marker)."""
    return sub.startswith("/") or sub.startswith("~/") or sub == "~"


def _absolute_endpoint(sub: str) -> str:
    """Normalize an absolute method template to a route path, dropping a leading ``~`` and
    reusing ``_join``'s slash-normalization (``~/metadata`` → ``/metadata``)."""
    return _join("", sub[1:] if sub.startswith("~") else sub)


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


def _action_name(fn: Function) -> str:
    """The action segment for a convention route: the ``[ActionName("x")]`` argument when
    present (it overrides the method name in MVC), else the method name itself."""
    for d in fn.decorators:
        if simple_attr_name(d.name) == "ActionName" and (name := _first_arg(d)):
            return name
    return fn.name


def _is_conventional_action(fn: Function) -> bool:
    """A public instance method that classic MVC treats as a GET action by convention.
    Excludes constructors/non-methods, non-public and static members, and ``[NonAction]``."""
    if fn.type != "method" or fn.isStatic or fn.visibility not in (None, "public"):
        return False
    return not any(simple_attr_name(d.name) == "NonAction" for d in fn.decorators)


def _route_statement(
    fn: Function, method: str, endpoint: str, text: str,
    auth_required: bool | None, guards: list[str] | None, seen: set[str],
) -> Statement:
    """Build one controller-route ``Statement`` (``nodeType="synthetic"`` — attribute/convention
    derived, no backing AST node). Shared by the attribute-route and convention-route paths."""
    return Statement(
        id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
        parentId=fn.id,
        nodeType="synthetic",
        semanticType="route",
        text=text,
        method=method,
        endpoint=endpoint,
        framework="aspnet",
        handler=fn.name,
        handlerLine=fn.startLine,
        routeKind="route",
        isRegex=False,
        authRequired=auth_required,
        guards=guards,
        requestDTO=_request_dto(fn),
        responseDTO=response_dto(fn.returnType),
        startLine=fn.startLine,
        endLine=fn.endLine,
        path=fn.path,
    )


def detect_controller_routes(record: FileRecord, index: Any = None) -> list[Statement]:
    # (prefix, chain guards, chain-resolved, has-any-[Route], class name, is-[ApiController]) per id
    controllers: dict[str, tuple[str, list[str], bool, bool, str, bool]] = {}
    for cls in record.classes:
        if not _is_controller(cls):
            continue
        route, guards, resolved = _resolve_chain(cls, index)
        base = _controller_token(route, cls.name) if route is not None else ""
        is_api = "ApiController" in {simple_attr_name(d.name) for d in cls.decorators}
        controllers[cls.id] = (base, guards, resolved, route is not None, cls.name, is_api)
    if not controllers:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        info = controllers.get(fn.parentId)
        if info is None:
            continue
        base, cls_guards, resolved, has_class_route, cls_name, is_api = info
        prefix_unknown = not has_class_route and not resolved  # inherited base we can't see
        method_route = _route_template(fn.decorators)
        fn_guards = _guards(fn.decorators)
        # [AllowAnonymous] (on the action or controller/base) always wins over [Authorize]
        anonymous = "AllowAnonymous" in cls_guards or "AllowAnonymous" in fn_guards
        auth_required = (not anonymous
                         and ("Authorize" in cls_guards or "Authorize" in fn_guards)) or None
        all_guards = (cls_guards + fn_guards) or None
        verb_attrs = [d for d in fn.decorators if simple_attr_name(d.name) in _HTTP_ATTRS]
        bound: set[tuple[str, str]] = set()  # one binding per (verb, endpoint): [HttpPost] twice is one
        for dec in verb_attrs:
            verb = _HTTP_ATTRS[simple_attr_name(dec.name)]
            for sub in _method_templates(_first_arg(dec), method_route):
                if _is_absolute(sub):
                    endpoint = _absolute_endpoint(sub)  # overrides the prefix → base-independent
                elif prefix_unknown:
                    # honest-null: a relative path needs the (unknowable) inherited prefix.
                    # Emit nothing rather than a fabricated endpoint — but only for this
                    # relative sub; an absolute sibling above is still emitted.
                    continue
                elif base or sub:
                    endpoint = _join(base, sub)
                else:
                    # verb attribute but no route anywhere → MVC convention /{controller}/{action}
                    endpoint = _convention_endpoint(cls_name, _action_name(fn))
                if (verb, endpoint) in bound:
                    continue
                bound.add((verb, endpoint))
                routes.append(_route_statement(
                    fn, verb, endpoint, f"[{dec.name}]", auth_required, all_guards, seen))
        # Classic MVC convention: a public action with NO [HttpX] attribute defaults to a GET at
        # /{controller}/{action} (or a bare method-level [Route], if any). Skipped for
        # [ApiController] (attribute routing is required there) and when the inherited prefix is
        # unknowable (honest-null).
        if not verb_attrs and not is_api and not prefix_unknown and _is_conventional_action(fn):
            sub = method_route or ""
            if _is_absolute(sub):
                endpoint = _absolute_endpoint(sub)
            elif base or sub:
                endpoint = _join(base, sub)
            else:
                endpoint = _convention_endpoint(cls_name, _action_name(fn))
            routes.append(_route_statement(
                fn, "GET", endpoint, f"(convention) {fn.name}", auth_required, all_guards, seen))
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
    ``MapGet/MapPost/…`` minimal APIs, ``MapHealthChecks`` and the GraphQL dev-UI middleware
    (``UseGraphQLPlayground``/…). The GraphQL transport mount (``UseGraphQL``/``MapGraphQL``) is
    wiring and has no record. Grammar-specific node/field names are supplied by the C#/VB caller."""
    routes: list[Statement] = []
    name_field = "name" if member_type == "member_access_expression" else "member"
    fn_field = "function" if invocation_type == "invocation_expression" else "target"
    decls: dict[str, dict[str, int | None]] | None = None  # built lazily, only for Map* calls
    groups: _Groups | None = None  # built lazily, only for Map* calls

    def visit(node: Node) -> None:
        nonlocal decls, groups
        for child in node.named_children:
            if child.type == invocation_type:
                func = child.child_by_field_name(fn_field)
                if func is not None and func.type == member_type:
                    method = _member_method_name(func, source, name_field)
                    spec = _endpoint_spec(method, child, root, source)
                    if spec is not None:
                        verb, framework, spec_endpoint = spec
                        endpoint: str | None = spec_endpoint
                        start = child.start_point[0] + 1
                        handler: str | None = None
                        handler_line: int | None = None
                        if method in _MAP_METHODS:
                            if decls is None:
                                decls = _declaration_lines(root, source)
                            handler, handler_line = _minimal_handler(
                                child, source, invocation_type, member_type, decls)
                            if groups is None:
                                groups = _Groups(root, source, invocation_type, member_type,
                                                 fn_field, name_field)
                            prefix = groups.prefix(_receiver(func, name_field), frozenset())
                            path_part = _minimal_path(child, source)
                            # Group prefix + route pattern. A group whose prefix exists but
                            # cannot be resolved, or a non-literal pattern → empty (Q8).
                            endpoint = (_join(prefix, path_part)
                                        if prefix is not None and path_part is not None else None)
                        routes.append(Statement(
                            id=disambiguate(statement_id(path, start, child.start_point[1]), seen_ids),
                            parentId=path,
                            nodeType=child.type,
                            semanticType="route",
                            text=node_text(child, source).split("\n", 1)[0][:200],
                            method=verb,
                            endpoint=endpoint,
                            framework=framework,
                            handler=handler,
                            handlerLine=handler_line,
                            routeKind="route",
                            isRegex=False,
                            startLine=start,
                            endLine=child.end_point[0] + 1,
                            path=path,
                        ))
            visit(child)

    visit(root)
    return routes


#: Inline handler delegates: no name; ``handlerLine`` is where they start.
_INLINE_DELEGATES = frozenset({"lambda_expression", "anonymous_method_expression"})
#: Declarations a method-group handler can name (C# methods and top-level local functions).
_DECLARATIONS = frozenset({"method_declaration", "local_function_statement"})


def _declaration_lines(root: Node, source: bytes) -> dict[str, dict[str, int | None]]:
    """Enclosing class name (``""`` for top-level local functions) → {method name → declaration
    line}. A name declared twice in the same scope (overloads) → None (honest-null)."""
    out: dict[str, dict[str, int | None]] = {}

    def walk(node: Node, owner: str) -> None:
        for c in node.named_children:
            if c.type in _DECLARATIONS:
                name = c.child_by_field_name("name")
                if name is not None:
                    scope = out.setdefault(owner, {})
                    key = node_text(name, source)
                    scope[key] = c.start_point[0] + 1 if key not in scope else None
                continue  # a nested local function is not addressable as a method group here
            if c.type == "class_declaration":
                name = c.child_by_field_name("name")
                walk(c, node_text(name, source) if name is not None else "?")
            else:
                walk(c, owner)

    walk(root, "")
    return out


def _minimal_handler(
    call: Node, source: bytes, invocation_type: str, member_type: str,
    decls: dict[str, dict[str, int | None]],
) -> tuple[str | None, int | None]:
    """(handler, handlerLine) of a ``MapGet``/``MapPost``/… call — its LAST argument (the
    delegate follows the route pattern in every minimal-API overload).

    - an inline lambda / anonymous method: no name; ``handlerLine`` is where it starts;
    - a method group (``GetOrders`` / ``Handlers.GetOrders``): its text as the name;
      ``handlerLine`` is its declaration when declared once in this file (a bare name: a
      top-level local function or a method of a class here; ``Type.Method``: a method of class
      ``Type`` here), else empty;
    - a wrapper call: unwrapped to its last argument. Anything else: (None, None)."""
    args = _positional_args(call)
    if len(args) < 2:
        return None, None
    last: Node | None = args[-1]
    while last is not None and last.type == invocation_type:
        inner = _positional_args(last)
        last = inner[-1] if inner else None
    if last is None:
        return None, None
    if last.type in _INLINE_DELEGATES:
        return None, last.start_point[0] + 1
    if last.type == "identifier":
        name = node_text(last, source)
        hits = [scope[name] for scope in decls.values() if name in scope]
        return name, hits[0] if len(hits) == 1 else None
    if last.type == member_type:
        text = node_text(last, source)
        owner, _, member = text.rpartition(".")
        return text, decls.get(owner.rsplit(".", 1)[-1], {}).get(member)
    return None, None


def _receiver(func: Node, name_field: str) -> Node | None:
    """The object a member call is made on (``api`` in ``api.MapGet``)."""
    obj = func.child_by_field_name("expression")
    if obj is not None:
        return obj
    name = func.child_by_field_name(name_field)
    return next((c for c in func.named_children if c != name), None)


class _Groups:
    """Route-group prefixes of one file: ``var api = app.MapGroup("/api")`` (nested groups,
    chained ``app.MapGroup("/x").MapGet(...)``, builder calls such as ``.RequireAuthorization()``
    in between). :meth:`prefix` → ``""`` for the app itself, the composed prefix of a group, or
    None when a group's prefix exists but cannot be resolved (a non-literal ``MapGroup`` argument,
    a variable assigned two different groups, a ``RouteGroupBuilder`` parameter whose caller is in
    another file)."""

    def __init__(self, root: Node, source: bytes, invocation_type: str, member_type: str,
                 fn_field: str, name_field: str) -> None:
        self.source, self.invocation_type, self.member_type = source, invocation_type, member_type
        self.fn_field, self.name_field = fn_field, name_field
        self.values: dict[str, Node | None] = {}  # variable → its group expression (None: ambiguous)
        self.params: set[str] = set()  # RouteGroupBuilder parameters (prefix set by the caller)
        self._collect(root)

    def _collect(self, node: Node) -> None:
        for c in node.named_children:
            if c.type == "variable_declarator":
                name = c.child_by_field_name("name")
                value = c.named_children[-1] if len(c.named_children) > 1 else None
                if name is not None and value is not None and self._is_group(value):
                    self._assign(node_text(name, self.source), value)
            elif c.type == "assignment_expression":
                left, right = c.child_by_field_name("left"), c.child_by_field_name("right")
                if left is not None and right is not None and left.type == "identifier" \
                        and self._is_group(right):
                    self._assign(node_text(left, self.source), right)
            elif c.type == "parameter":
                ptype, pname = c.child_by_field_name("type"), c.child_by_field_name("name")
                if ptype is not None and pname is not None \
                        and node_text(ptype, self.source).rsplit(".", 1)[-1] == "RouteGroupBuilder":
                    self.params.add(node_text(pname, self.source))
            self._collect(c)

    def _assign(self, name: str, value: Node) -> None:
        prev = self.values.get(name, value)
        same = prev is not None and node_text(prev, self.source) == node_text(value, self.source)
        self.values[name] = value if name not in self.values or same else None

    def _call_name(self, call: Node) -> str:
        func = call.child_by_field_name(self.fn_field)
        if func is None or func.type != self.member_type:
            return ""
        return _member_method_name(func, self.source, self.name_field)

    def _is_group(self, expr: Node) -> bool:
        """Whether an expression is a ``MapGroup`` call (possibly followed by builder calls)."""
        while expr.type == self.invocation_type:
            if self._call_name(expr) == "MapGroup":
                return True
            func = expr.child_by_field_name(self.fn_field)
            inner = _receiver(func, self.name_field) if func is not None else None
            if inner is None:
                return False
            expr = inner
        return False

    def prefix(self, expr: Node | None, seen: frozenset[str]) -> str | None:
        if expr is None:
            return ""
        if expr.type == self.invocation_type:
            func = expr.child_by_field_name(self.fn_field)
            inner = _receiver(func, self.name_field) if func is not None else None
            base = self.prefix(inner, seen)
            if self._call_name(expr) != "MapGroup":
                return base  # .RequireAuthorization() / .WithTags(...) return the same group
            sub = _minimal_path(expr, self.source)
            return _join(base, sub) if base is not None and sub is not None else None
        if expr.type == "identifier":
            name = node_text(expr, self.source)
            if name in self.params:
                return None
            if name not in self.values:
                return ""  # the app / endpoint builder itself
            value = self.values[name]
            return self.prefix(value, seen | {name}) if value is not None and name not in seen \
                else None
        return ""


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


def _minimal_path(call: Node, source: bytes) -> str | None:
    """Route template = the FIRST argument when it is a string literal. The args that follow
    (the handler delegate/lambda) may hold their own string literals — never mistake those for
    the path; return None when the first arg isn't itself a literal (``PathOf("x")``,
    ``Config.Prefix``): absent beats a fabricated endpoint."""
    args = call.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    if first is None:
        return None
    inner = first
    while inner.type != "string_literal" and len(inner.named_children) == 1:
        inner = inner.named_children[0]  # argument / expression wrappers (VB nests several)
    return _literal(inner, source) if inner.type == "string_literal" else None


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
) -> tuple[list[Statement], bool]:
    """AST-walk for MVC / Web-API **convention-route registrations** —
    ``routes.MapRoute(name, url, defaults)`` / ``config.Routes.MapHttpRoute(…)`` declared in
    ``RouteConfig`` / ``Global.asax`` / an ``AreaRegistration``. A template that names one
    action — no ``{controller}`` / ``{action}`` placeholder, and a literal default ``controller``
    and ``action`` — is a binding: a ``route`` (method ``ANY``, since a convention route matches
    any verb) with ``Controller.Action`` as the handler. A convention template
    (``{controller}/{action}/{id}``, Web API ``api/{controller}/{id}`` where the verb picks the
    action) identifies no single handler: it is wiring and its statement stays plain.
    Grammar-specific node/field names are supplied by the C#/VB caller. Returns the bindings and
    whether any registration was seen — a wiring-only file (``RouteConfig.cs``) is still ASP.NET
    code and keeps its ``aspnet`` label."""
    routes: list[Statement] = []
    seen_registration = False
    name_field = "name" if member_type == "member_access_expression" else "member"
    fn_field = "function" if invocation_type == "invocation_expression" else "target"

    def visit(node: Node) -> None:
        nonlocal seen_registration
        for child in node.named_children:
            if child.type == invocation_type:
                func = child.child_by_field_name(fn_field)
                if func is not None and func.type == member_type:
                    name_node = func.child_by_field_name(name_field)
                    method = node_text(name_node, source) if name_node is not None else ""
                    url = _maproute_url(child, source) if method in _MAP_ROUTE_METHODS else None
                    text = node_text(child, source) if url else ""
                    seen_registration = seen_registration or url is not None
                    ctrl, action = _CTRL_RE.search(text), _ACTION_RE.search(text)
                    one_action = (url is not None and ctrl is not None and action is not None
                                  and "{controller}" not in url and "{action}" not in url)
                    if url and one_action and ctrl is not None and action is not None:
                        handler = f"{ctrl.group(1)}.{action.group(1)}"
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
    return routes, seen_registration
