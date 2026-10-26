"""Go HTTP router analysis, shared by the Go router detectors (``parsers/go_*/``).

A Go router framework is described by a :class:`RouterSpec` — its import path, router types,
constructors, verb methods and grouping methods — and this module does the rest the same way
for every framework: decide which expressions are routers (from how they are bound, never from
a variable's name), carry each router's path prefix, and turn route calls into ``route`` emits.

**What is a router.** A value of the framework's package bound by

- a constructor (``gin.Default()``, ``chi.NewRouter()``, ``http.NewServeMux()``);
- a parameter, or a struct field, declared with a router type (``r *gin.RouterGroup``,
  ``s.router`` where ``Server.router`` is ``*gin.Engine`` — in this file or another of the
  package, via ``go.declarations``);
- a grouping call on a router (``r.Group("/v1")``, ``r.PathPrefix("/api").Subrouter()``,
  chi ``r.Route("/x", func(r chi.Router) {…})``).

**Prefixes.** A router created by a constructor is served at the root, unless its function
returns it (``func adminRouter() chi.Router``) or it is mounted (``r.Mount("/admin", sub)``). A
router parameter is served wherever the callers' routers are: every call ``F(router)`` in the
repo is an edge recorded by the framework's ``<framework>.router-prefixes`` fact, and the fact
folds the edges into one prefix per parameter — empty (unknown) when the callers disagree or a
caller's prefix is itself unknown, and the root when nothing in the repo calls ``F`` (as for a
Fastify plugin nothing registers). A field of a group type, or a router parameter of a closure
nothing groups, has an unknown prefix. An unknown prefix or path part leaves ``endpoint`` empty —
never a partial address.

**Handlers.** The last argument of the route call, unwrapped through wrapper calls
(``http.HandlerFunc(h)``, ``auth(h)``): a function or method value names the handler and is
located through ``go.declarations``; a function literal has no name and ``handlerLine`` is
where it starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from tree_sitter import Node

from ..additive import DetectContext, Detection, FactSpec, index_fact, register_fact
from ..contract import HTTP_METHODS
from ..emits import Emit
from ..handler_sites import handler_location
from ..treesitter import node_text
from .declarations import Declarations, declarations, package_dir, receiver_type
from .imports import GoIndex, import_aliases

GroupKind = Literal["child", "same", "closure_child", "closure_same", "mount"]


@dataclass(frozen=True)
class RouterSpec:
    """One Go router framework."""

    #: ``framework`` on records, the detector's name suffix and the file label.
    name: str
    #: Package import path; versioned paths (``…/v5``) match too.
    import_path: str
    #: Router type names of the package (``RouterGroup``, ``Router``, ``ServeMux``).
    router_types: frozenset[str]
    #: Of those, the application types: a struct field of one is the app, served at the root.
    app_types: frozenset[str]
    #: Package functions that create a root router.
    constructors: frozenset[str]
    #: Route method → HTTP verb (``"ANY"`` for an every-verb method, ``None`` when the method
    #: carries no verb — the record's ``method`` stays empty).
    verbs: dict[str, str | None]
    #: Route methods whose first argument is the verb: method → index of the path argument.
    verb_arg: dict[str, int] = field(default_factory=dict)
    #: Methods that serve files under a path (``Static``): framework-served, ``GET``, no handler.
    statics: frozenset[str] = frozenset()
    #: Grouping methods → what they return or pass to their closure.
    groups: dict[str, GroupKind] = field(default_factory=dict)
    #: Package-level route functions on the default router (``http.HandleFunc``).
    package_routes: bool = False
    #: (method, path) → (method, path): reads a verb written into the pattern (Go 1.22 ServeMux).
    pattern: Callable[[str | None, str | None], tuple[str | None, str | None]] | None = None
    #: The route call → its verbs from a chain on the call (gorilla ``.Methods("GET")``), or None.
    chain_verbs: Callable[[Node, bytes], list[str] | None] | None = None
    #: Package functions that only forward the handler they are given (``http.HandlerFunc``,
    #: ``gin.WrapF``). Any other package's call makes the handler (``http.FileServer``).
    adapters: frozenset[str] = frozenset()
    #: The handler is the argument after the path (Echo: ``GET(path, h, mw…)``); else the last.
    handler_after_path: bool = False

    @property
    def guard(self) -> bytes:
        return self.import_path.encode()

    def fact_name(self) -> str:
        return f"{self.name}.router-prefixes"

    def owns(self, import_path: str) -> bool:
        if import_path == self.import_path:
            return True
        rest = import_path[len(self.import_path) + 1:] if import_path.startswith(self.import_path + "/") else ""
        return rest[:1] == "v" and rest[1:].isdigit()


# --- prefixes ---------------------------------------------------------------------------------

#: A router's base: ROOT, a parameter or a returned router resolved through the fact, or UNKNOWN.
ROOT = ("root",)
UNKNOWN = ("unknown",)
Base = tuple  # ROOT | UNKNOWN | ("param", FuncKey, idx) | ("ret", FuncKey)


def join(prefix: str | None, path: str | None) -> str | None:
    """``/api`` + ``/users`` → ``/api/users``; either unknown → None."""
    if prefix is None or path is None:
        return None
    if not path:
        return prefix
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


@dataclass
class Origin:
    """Where a router's prefix starts. Shared by a router and every group made from it, so a
    later ``Mount`` of a constructed router moves all of them."""

    base: Base
    rel: str | None = ""
    mounted: bool = False


@dataclass
class Router:
    spec: RouterSpec
    origin: Origin
    rel: str | None = ""  # path below the origin

    def child(self, path: str | None) -> "Router":
        return Router(self.spec, self.origin, join(self.rel, path))


# --- per-file analysis ------------------------------------------------------------------------

FuncKey = tuple[str, str, str]  # (package dir, receiver type or "", name)
CalleeRef = tuple  # ("func", import path | None, name) | ("method", name)


@dataclass
class RouteFound:
    call: Node
    spec: RouterSpec
    router: Router
    method: str | None
    path: str | None
    handler: Node | None
    served: bool = False  # framework-served (Static): no handler


@dataclass
class Edges:
    """What a file contributes to a ``router-prefixes`` fact."""

    #: (callee, target kind, arg index, caller package dir, caller base, rel). A ``param`` edge
    #: gives a callee's router parameter its prefix; a ``ret`` edge mounts a callee's returned
    #: router.
    edges: list[tuple[CalleeRef, str, int, str, Base, str | None]] = field(default_factory=list)
    #: Router parameters declared here: (function key, index).
    params: list[tuple[FuncKey, int]] = field(default_factory=list)
    #: Functions declared here that return a router they construct.
    rets: list[FuncKey] = field(default_factory=list)


def _string(node: Node | None, source: bytes, consts: dict[str, str]) -> str | None:
    """A string literal, a constant of this file, or a ``+`` of those; else None."""
    if node is None:
        return None
    if node.type in {"interpreted_string_literal", "raw_string_literal"}:
        return node_text(node, source)[1:-1]
    if node.type == "identifier":
        return consts.get(node_text(node, source))
    if node.type == "binary_expression" and node_text(node.child_by_field_name("operator"), source) == "+":
        left = _string(node.child_by_field_name("left"), source, consts)
        right = _string(node.child_by_field_name("right"), source, consts)
        return left + right if left is not None and right is not None else None
    if node.type == "parenthesized_expression" and node.named_children:
        return _string(node.named_children[0], source, consts)
    return None


def _consts(root: Node, source: bytes) -> dict[str, str]:
    """Package-level string constants of this file (``const base = "/api"``)."""
    out: dict[str, str] = {}
    for decl in root.named_children:
        if decl.type != "const_declaration":
            continue
        for spec in decl.named_children:
            if spec.type != "const_spec":
                continue
            names = spec.children_by_field_name("name")
            value = spec.child_by_field_name("value")
            values = value.named_children if value is not None else []
            for name, val in zip(names, values):
                text = _string(val, source, out)
                if text is not None:
                    out[node_text(name, source)] = text
    return out


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [a for a in args.named_children if a.type != "comment"] if args is not None else []


def _selector(call: Node) -> tuple[Node, str] | None:
    """``x.M(...)`` → (x, "M")."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return None
    operand, fld = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
    if operand is None or fld is None:
        return None
    return operand, fld.text.decode("utf-8", "replace")


def _returned_names(body: Node | None, source: bytes) -> set[str]:
    out: set[str] = set()
    if body is None:
        return out
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type == "func_literal":
            continue
        if node.type == "return_statement":
            for expr in node.named_children:
                items = expr.named_children if expr.type == "expression_list" else [expr]
                out.update(node_text(i, source) for i in items if i.type == "identifier")
        stack.extend(node.named_children)
    return out


class _Analyzer:
    def __init__(self, root: Node, source: bytes, rel: str, specs: list[RouterSpec],
                 decls: Declarations, index: GoIndex | None) -> None:
        self.source, self.rel, self.pkg = source, rel, package_dir(rel)
        self.aliases = import_aliases(root, source)
        self.spec_of = {a: s for a, ip in self.aliases.items() for s in specs if s.owns(ip)}
        self.consts = _consts(root, source)
        self.decls, self.index = decls, index
        self.local_fields = self._local_fields(root)
        self.routes: list[RouteFound] = []
        self.edges = Edges()
        self.root = root

    # -- types ---------------------------------------------------------------------------------

    def _router_type(self, type_text: str) -> tuple[RouterSpec, str] | None:
        """``*gin.RouterGroup`` → (gin spec, "RouterGroup") when that is a router type."""
        alias, dot, name = type_text.lstrip("*").partition(".")
        spec = self.spec_of.get(alias) if dot else None
        name = name.split("[", 1)[0]
        return (spec, name) if spec is not None and name in spec.router_types else None

    def _spec_for_qualified(self, qualified: tuple[str, str]) -> tuple[RouterSpec, str] | None:
        import_path, name = qualified
        for spec in {id(s): s for s in self.spec_of.values()}.values():
            if spec.owns(import_path) and name in spec.router_types:
                return spec, name
        return None

    def _local_fields(self, root: Node) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for decl in root.named_children:
            if decl.type != "type_declaration":
                continue
            for spec in decl.named_children:
                tnode = spec.child_by_field_name("type") if spec.type == "type_spec" else None
                if tnode is None or tnode.type != "struct_type":
                    continue
                struct = node_text(spec.child_by_field_name("name"), self.source)
                for body in tnode.named_children:
                    for fdecl in body.named_children:
                        ftype = fdecl.child_by_field_name("type") if fdecl.type == "field_declaration" else None
                        if ftype is None:
                            continue
                        for name in fdecl.children_by_field_name("name"):
                            out[(struct, node_text(name, self.source))] = node_text(ftype, self.source)
        return out

    def _field_router(self, struct: str | None, fname: str) -> Router | None:
        if struct is None:
            return None
        found = None
        if (struct, fname) in self.local_fields:
            found = self._router_type(self.local_fields[(struct, fname)])
        else:
            qualified = self.decls.fields.get((self.pkg, struct, fname))
            found = self._spec_for_qualified(qualified) if qualified else None
        if found is None:
            return None
        spec, tname = found
        return Router(spec, Origin(ROOT if tname in spec.app_types else UNKNOWN))

    # -- expressions ---------------------------------------------------------------------------

    def eval(self, node: Node | None, env: dict[str, Any]) -> Router | None:
        if node is None:
            return None
        kind = node.type
        if kind in {"parenthesized_expression", "unary_expression"}:
            inner = node.child_by_field_name("operand") or (node.named_children[0] if node.named_children else None)
            return self.eval(inner, env)
        if kind == "identifier":
            value = env.get(node_text(node, self.source))
            return value if isinstance(value, Router) else None
        if kind == "selector_expression":
            operand, fld = node.child_by_field_name("operand"), node.child_by_field_name("field")
            if operand is None or fld is None or operand.type != "identifier":
                return None
            struct = env.get(("type", node_text(operand, self.source)))
            return self._field_router(struct, node_text(fld, self.source))
        if kind != "call_expression":
            return None
        sel = _selector(node)
        if sel is None:
            return None
        operand, method = sel
        if operand.type == "identifier":
            spec = self.spec_of.get(node_text(operand, self.source))
            if spec is not None and operand.text.decode() not in env:
                return Router(spec, Origin(ROOT)) if method in spec.constructors else None
        router = self.eval(operand, env)
        if router is None:
            return None
        group = router.spec.groups.get(method)
        if group == "child":
            args = _args(node)
            return router.child(_string(args[0], self.source, self.consts) if args else None)
        if group == "same":
            return router
        return None

    # -- functions -----------------------------------------------------------------------------

    def run(self) -> None:
        globals_: dict[str, Any] = {}
        for decl in self.root.named_children:
            if decl.type == "var_declaration":
                self._bind_vars(decl, globals_, returned=set(), key=None)
        for decl in self.root.named_children:
            if decl.type in {"function_declaration", "method_declaration"}:
                name = decl.child_by_field_name("name")
                key = (self.pkg, receiver_type(decl, self.source), node_text(name, self.source))
                env = dict(globals_)
                recv = decl.child_by_field_name("receiver")
                if recv is not None:
                    self._bind_params(recv, env, key=None, typed_only=True)
                self._bind_params(decl.child_by_field_name("parameters"), env, key=key)
                body = decl.child_by_field_name("body")
                self._walk(body, env, key, _returned_names(body, self.source))

    def _bind_params(self, params: Node | None, env: dict[str, Any], *, key: FuncKey | None,
                     typed_only: bool = False, given: Router | None = None) -> None:
        if params is None:
            return
        idx = -1
        for decl in params.named_children:
            if decl.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
                continue
            tnode = decl.child_by_field_name("type")
            ttext = node_text(tnode, self.source) if tnode is not None else ""
            names = decl.children_by_field_name("name") or [None]
            for name in names:
                idx += 1
                if name is None:
                    continue
                pname = node_text(name, self.source)
                env[("type", pname)] = ttext.lstrip("*").split("[", 1)[0]
                if typed_only:
                    continue
                found = self._router_type(ttext)
                if found is None:
                    continue
                spec = found[0]
                if given is not None and given.spec is spec:
                    env[pname] = given
                elif key is not None:
                    env[pname] = Router(spec, Origin(("param", key, idx)))
                    self.edges.params.append((key, idx))
                else:
                    env[pname] = Router(spec, Origin(UNKNOWN))

    def _bind(self, name: str, value: Node | None, env: dict[str, Any], returned: set[str],
              key: FuncKey | None) -> None:
        router = self.eval(value, env)
        if router is not None:
            if (name in returned and key is not None and router.origin.base == ROOT
                    and router.rel == "" and not router.origin.mounted):
                router.origin.base = ("ret", key)
                self.edges.rets.append(key)
            env[name] = router
        elif value is not None:
            env.pop(name, None)
            typ = self._value_type(value)
            if typ:
                env[("type", name)] = typ

    def _value_type(self, value: Node) -> str | None:
        """``&Handler{…}`` / ``Handler{…}`` → ``Handler`` (``pkg.Handler`` kept qualified)."""
        node = value
        if node.type == "unary_expression":
            node = node.child_by_field_name("operand") or node
        if node.type == "composite_literal":
            tnode = node.child_by_field_name("type")
            return node_text(tnode, self.source).split("[", 1)[0] if tnode is not None else None
        return None

    def _bind_vars(self, decl: Node, env: dict[str, Any], returned: set[str], key: FuncKey | None) -> None:
        specs = [c for c in decl.named_children if c.type == "var_spec"]
        for group in decl.named_children:
            if group.type == "var_spec_list":
                specs.extend(c for c in group.named_children if c.type == "var_spec")
        for spec in specs:
            names = spec.children_by_field_name("name")
            value = spec.child_by_field_name("value")
            values = value.named_children if value is not None else []
            tnode = spec.child_by_field_name("type")
            for i, name in enumerate(names):
                pname = node_text(name, self.source)
                if i < len(values):
                    self._bind(pname, values[i], env, returned, key)
                elif tnode is not None:
                    env[("type", pname)] = node_text(tnode, self.source).lstrip("*")

    def _walk(self, node: Node | None, env: dict[str, Any], key: FuncKey | None,
              returned: set[str]) -> None:
        if node is None:
            return
        for child in node.named_children:
            kind = child.type
            if kind in {"short_var_declaration", "assignment_statement"}:
                self._walk(child.child_by_field_name("right"), env, key, returned)
                left, right = child.child_by_field_name("left"), child.child_by_field_name("right")
                lnames = left.named_children if left is not None else []
                rvalues = right.named_children if right is not None else []
                if len(lnames) == len(rvalues):
                    for lhs, rhs in zip(lnames, rvalues):
                        if lhs.type == "identifier":
                            self._bind(node_text(lhs, self.source), rhs, env, returned, key)
                continue
            if kind == "var_declaration":
                self._walk(child, env, key, returned)
                self._bind_vars(child, env, returned, key)
                continue
            if kind == "func_literal":
                inner = dict(env)
                self._bind_params(child.child_by_field_name("parameters"), inner, key=None)
                self._walk(child.child_by_field_name("body"), inner, None, set())
                continue
            if kind == "call_expression":
                if self._call(child, env, key, returned):
                    continue
            self._walk(child, env, key, returned)

    def _call(self, call: Node, env: dict[str, Any], key: FuncKey | None, returned: set[str]) -> bool:
        """Handle one call. True when its arguments were walked here."""
        sel = _selector(call)
        args = _args(call)
        if sel is not None:
            operand, method = sel
            router = None
            if operand.type == "identifier":
                spec = self.spec_of.get(node_text(operand, self.source))
                if spec is not None and node_text(operand, self.source) not in env:
                    if spec.package_routes and method in spec.verbs:
                        router = Router(spec, Origin(ROOT))
                    else:
                        return False
            if router is None:
                router = self.eval(operand, env)
            if router is not None:
                return self._router_call(call, router, method, args, env, key, returned)
        self._edges(call, args, env)
        return False

    def _router_call(self, call: Node, router: Router, method: str, args: list[Node],
                     env: dict[str, Any], key: FuncKey | None, returned: set[str]) -> bool:
        spec = router.spec
        group = spec.groups.get(method)
        if group in {"closure_child", "closure_same"}:
            child = router.child(_string(args[0], self.source, self.consts) if args else None) \
                if group == "closure_child" else router
            fn = args[-1] if args else None
            for arg in args[:-1]:
                self._walk_expr(arg, env, key, returned)
            if fn is not None and fn.type == "func_literal":
                inner = dict(env)
                self._bind_params(fn.child_by_field_name("parameters"), inner, key=None, given=child)
                self._walk(fn.child_by_field_name("body"), inner, None, set())
            return True
        if group == "mount" and len(args) >= 2:
            self._mount(router.child(_string(args[0], self.source, self.consts)), args[1], env)
            return False
        if method in spec.verbs or method in spec.verb_arg or method in spec.statics:
            self._route(call, router, method, args)
        return False

    def _walk_expr(self, node: Node, env: dict[str, Any], key: FuncKey | None, returned: set[str]) -> None:
        if node.type == "call_expression":
            if not self._call(node, env, key, returned):
                self._walk(node, env, key, returned)
        elif node.type == "func_literal":
            inner = dict(env)
            self._bind_params(node.child_by_field_name("parameters"), inner, key=None)
            self._walk(node.child_by_field_name("body"), inner, None, set())
        else:
            self._walk(node, env, key, returned)

    def _mount(self, at: Router, target: Node, env: dict[str, Any]) -> None:
        """``r.Mount("/admin", sub)`` / ``r.Mount("/admin", adminRouter())``."""
        sub = self.eval(target, env)
        if sub is not None:
            origin = sub.origin
            if origin.base == ROOT and origin.rel == "" and not origin.mounted:
                origin.base, origin.rel, origin.mounted = at.origin.base, join(at.origin.rel, at.rel), True
            elif origin.mounted:
                origin.base = UNKNOWN  # mounted twice
            return
        callee = self._callee(target) if target.type == "call_expression" else None
        if callee is not None:
            self.edges.edges.append((callee, "ret", 0, self.pkg, at.origin.base, join(at.origin.rel, at.rel)))

    def _callee(self, call: Node) -> CalleeRef | None:
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return ("func", None, node_text(fn, self.source))
        if fn.type == "selector_expression":
            operand, fld = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
            if operand is None or fld is None:
                return None
            name = node_text(fld, self.source)
            if operand.type == "identifier" and node_text(operand, self.source) in self.aliases:
                if node_text(operand, self.source) in self.spec_of:
                    return None
                return ("func", self.aliases[node_text(operand, self.source)], name)
            return ("method", name)
        return None

    def _edges(self, call: Node, args: list[Node], env: dict[str, Any]) -> None:
        """A call passing a router to a function of the repo → a ``param`` edge per router."""
        routers = [(i, self.eval(a, env)) for i, a in enumerate(args)]
        routers = [(i, r) for i, r in routers if r is not None]
        if not routers:
            return
        callee = self._callee(call)
        if callee is None:
            return
        for i, router in routers:
            self.edges.edges.append(
                (callee, "param", i, self.pkg, router.origin.base, join(router.origin.rel, router.rel)))

    def _route(self, call: Node, router: Router, method: str, args: list[Node]) -> None:
        spec = router.spec
        if method in spec.statics:
            path = _string(args[0], self.source, self.consts) if args else None
            self.routes.append(RouteFound(call, spec, router, "GET", path, None, served=True))
            return
        if method in spec.verb_arg:
            verb_node = args[0] if args else None
            verb = _string(verb_node, self.source, self.consts) if verb_node is not None else None
            if verb is None and verb_node is not None and verb_node.type == "selector_expression":
                verb = _http_constant(node_text(verb_node, self.source))
            path_idx = spec.verb_arg[method]
            path = _string(args[path_idx], self.source, self.consts) if len(args) > path_idx else None
            verbs: list[str | None] = [verb.upper() if verb else None]
        else:
            path_idx = 0
            path = _string(args[0], self.source, self.consts) if args else None
            verbs = [spec.verbs[method]]
            chained = spec.chain_verbs(call, self.source) if spec.chain_verbs else None
            if chained:
                verbs = list(chained)
        if spec.handler_after_path:
            handler = args[path_idx + 1] if len(args) > path_idx + 1 else None
        else:
            handler = args[-1] if len(args) > path_idx + 1 else None
        for verb in verbs:
            v, p = spec.pattern(verb, path) if spec.pattern else (verb, path)
            if v not in HTTP_METHODS and v != "ANY":
                v = None  # CONNECT / TRACE / a verb we can't read: no method value
            self.routes.append(RouteFound(call, spec, router, v, p, handler))


def _http_constant(text: str) -> str | None:
    """``http.MethodGet`` → ``GET``."""
    name = text.rpartition(".")[2]
    if name.startswith("Method") and name[6:].upper() in HTTP_METHODS:
        return name[6:].upper()
    return None


def analyze(root: Node, source: bytes, rel: str, specs: list[RouterSpec],
            index: GoIndex | None) -> _Analyzer:
    analyzer = _Analyzer(root, source, rel, specs, declarations(index), index)
    analyzer.run()
    return analyzer


# --- the prefix fact --------------------------------------------------------------------------

def _resolve_prefixes(values: list[Edges], index: Any) -> dict[Base, str | None] | None:
    """Fold every file's edges into one prefix per router parameter / returned router: the
    single prefix all callers agree on; None when they disagree, a caller's prefix is unknown, or
    the callee is ambiguous; ``""`` (served at its own paths) when nothing calls it."""
    params = {(k, i) for v in values for k, i in v.params}
    rets = {k for v in values for k in v.rets}
    method_params: dict[tuple[str, int], set[FuncKey]] = {}
    for key, idx in params:
        if key[1]:
            method_params.setdefault((key[2], idx), set()).add(key)
    method_rets: dict[str, set[FuncKey]] = {}
    for key in rets:
        if key[1]:
            method_rets.setdefault(key[2], set()).add(key)

    incoming: dict[Base, list[tuple[Base, str | None]]] = {}
    ambiguous: set[Base] = set()
    for value in values:
        for callee, kind, idx, caller_pkg, base, rel in value.edges:
            keys: set[FuncKey]
            if callee[0] == "func":
                import_path, name = callee[1], callee[2]
                pkg = caller_pkg if import_path is None else (
                    index.package_dir(import_path) if isinstance(index, GoIndex) else None)
                if pkg is None:
                    continue
                keys = {(pkg, "", name)}
            else:
                keys = (method_params.get((callee[1], idx), set()) if kind == "param"
                        else method_rets.get(callee[1], set()))
            targets = [("param", k, idx) if kind == "param" else ("ret", k) for k in keys]
            if len(targets) > 1:
                ambiguous.update(targets)
                continue
            for target in targets:
                incoming.setdefault(target, []).append((base, rel))

    memo: dict[Base, str | None] = {}
    active: set[Base] = set()

    def prefix(target: Base) -> str | None:
        if target in memo:
            return memo[target]
        if target in ambiguous or target in active:
            return None
        active.add(target)
        results: set[str | None] = set()
        for base, rel in incoming.get(target, []):
            if base == ROOT:
                results.add(rel)
            elif base == UNKNOWN:
                results.add(None)
            else:
                results.add(join(prefix(base), rel))
        active.discard(target)
        memo[target] = "" if not results else (results.pop() if len(results) == 1 else None)
        return memo[target]

    targets = {("param", k, i) for k, i in params} | {("ret", k) for k in rets} | set(incoming)
    return {t: prefix(t) for t in targets} or None


def register_router_fact(spec: RouterSpec) -> None:
    def collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> Edges | None:
        found = analyze(root, source, rel, [spec], None).edges
        return found if found.edges or found.params or found.rets else None

    register_fact(FactSpec(name=spec.fact_name(), language="go", gate=(spec.guard,),
                           collect=collect, resolve=_resolve_prefixes))


# --- the detector -----------------------------------------------------------------------------

def _endpoint(router: Router, path: str | None, prefixes: dict[Base, str | None] | None) -> str | None:
    base = router.origin.base
    if base == ROOT:
        start: str | None = ""
    elif base == UNKNOWN:
        start = None
    else:
        start = (prefixes or {}).get(base, "")
    endpoint = join(join(start, router.origin.rel), join(router.rel, path))
    return None if endpoint is None else (endpoint or "/")


def _unwrap_handler(node: Node | None, analyzer: "_Analyzer", spec: RouterSpec) -> Node | None:
    """``http.HandlerFunc(h)`` / ``auth(h)`` → ``h`` (the last argument, repeatedly). A call of
    another package that is not one of the spec's adapters makes the handler itself
    (``http.FileServer(dir)``, ``expvar.Handler()``): framework-served, no handler."""
    while node is not None and node.type == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "selector_expression":
            operand = fn.child_by_field_name("operand")
            name = node_text(fn.child_by_field_name("field"), analyzer.source)
            alias = node_text(operand, analyzer.source) if operand is not None else ""
            if alias in analyzer.aliases and not (spec.owns(analyzer.aliases[alias]) and name in spec.adapters):
                return None
        args = _args(node)
        node = args[-1] if args else None
    return node


def _locate(analyzer: _Analyzer, handler: Node, env_types: dict[str, str]) -> tuple[str | None, int | None, str | None]:
    """(handler name, handlerLine, handlerPath)."""
    source, decls = analyzer.source, analyzer.decls
    if handler.type == "func_literal":
        return None, handler.start_point[0] + 1, None
    name = node_text(handler, source)
    site = None
    if handler.type == "identifier":
        site = decls.functions.get((analyzer.pkg, "", name))
    elif handler.type == "selector_expression":
        operand = handler.child_by_field_name("operand")
        fld = node_text(handler.child_by_field_name("field"), source)
        recv = node_text(operand, source) if operand is not None else ""
        if recv in analyzer.aliases and analyzer.index is not None:
            pkg = analyzer.index.package_dir(analyzer.aliases[recv])
            site = decls.functions.get((pkg, "", fld)) if pkg is not None else None
        else:
            typ = env_types.get(recv)
            if typ:
                alias, dot, tname = typ.partition(".")
                pkg = analyzer.pkg
                if dot:
                    pkg = (analyzer.index.package_dir(analyzer.aliases.get(alias, ""))
                           if analyzer.index is not None else None)
                else:
                    tname = typ
                site = decls.functions.get((pkg, tname, fld)) if pkg is not None else None
            else:
                sites = decls.methods.get(fld, ())
                site = sites[0] if len(sites) == 1 else None
    else:
        return None, None, None
    line, path = handler_location(site, analyzer.rel)
    return name, line, path


def detect_routes(dc: DetectContext, spec: RouterSpec) -> Detection | None:
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    analyzer = analyze(dc.root, dc.source, dc.path, [spec], index)
    if not analyzer.routes:
        return None
    prefixes = index_fact(index, spec.fact_name())
    types = _types_by_function(analyzer)
    found: list[Emit] = []
    for route in analyzer.routes:
        fields: dict[str, Any] = {
            "semanticType": "route", "framework": spec.name, "routeKind": "route",
            "method": route.method, "endpoint": _endpoint(route.router, route.path, prefixes),
        }
        fill: dict[str, Any] = {}
        handler = _unwrap_handler(route.handler, analyzer, spec) if not route.served else None
        if handler is not None:
            name, line, path = _locate(analyzer, handler, types(route.call))
            fill = {"handler": name, "handlerLine": line, "handlerPath": path}
        found.append(Emit(route.call, fields, fill=fill, enrich="any"))
    return Detection(found, label=spec.name)


def _types_by_function(analyzer: _Analyzer) -> Callable[[Node], dict[str, str]]:
    """Route call → the declared types of the names in its function (params, ``&T{}``)."""
    cache: dict[int, dict[str, str]] = {}

    def types(call: Node) -> dict[str, str]:
        fn = call.parent
        while fn is not None and fn.type not in {"function_declaration", "method_declaration"}:
            fn = fn.parent
        if fn is None:
            return {}
        if fn.start_byte not in cache:
            env: dict[Any, Any] = {}
            recv = fn.child_by_field_name("receiver")
            if recv is not None:
                analyzer._bind_params(recv, env, key=None, typed_only=True)
            analyzer._bind_params(fn.child_by_field_name("parameters"), env, key=None, typed_only=True)
            stack = [fn.child_by_field_name("body")]
            while stack:
                node = stack.pop()
                if node is None:
                    continue
                if node.type in {"short_var_declaration", "assignment_statement"}:
                    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                    if left is not None and right is not None and len(left.named_children) == len(right.named_children):
                        for lhs, rhs in zip(left.named_children, right.named_children):
                            typ = analyzer._value_type(rhs)
                            if lhs.type == "identifier" and typ:
                                env[("type", node_text(lhs, analyzer.source))] = typ
                stack.extend(node.named_children)
            cache[fn.start_byte] = {k[1]: v for k, v in env.items() if isinstance(k, tuple)}
        return cache[fn.start_byte]

    return types
