"""Fastify instances in a file, and the prefixes plugins are served under — the
``fastify.plugin-prefixes`` fact (see ``parsers/additive.py``).

Fastify routes are registered on an *instance*. cog only treats a name as one when the code binds
it to Fastify — never by the name alone::

    const app = Fastify()                                  // an app: no prefix
    app.register(usersPlugin, { prefix: '/users' })        // WIRING: /users for usersPlugin's routes
    app.register(AutoLoad, { dir: join(__dirname, 'routes') })  // WIRING: a prefix per folder
    export default async function (fastify: FastifyInstance) {  // a plugin: `fastify` is an instance
      fastify.get('/:id', getUser)                         // → GET /users/:id
    }

:func:`analyze` finds the instances of one file (:class:`Scope`): an app made by the ``fastify``
package's factory, a plugin function passed to ``register`` here, and a plugin the file exports.
Each scope's prefix is relative to an *anchor* — the app (no prefix), an exported plugin (whatever
prefix the repo registers it under), or unknown — plus the inline ``register`` prefixes of this
file in between.

While the TypeScript index is built, ``_collect`` reads each file's registrations of another
file's plugin and its ``@fastify/autoload`` registrations; ``_resolve`` expands each autoload
folder the way ``@fastify/autoload`` does (a prefix per sub-folder, ``index`` files, ``autoPrefix``
/ ``prefixOverride``, ``autohooks``) and composes the prefixes from the app down. A plugin
registered under two different prefixes, or under one that can't be resolved, gets ``None``: its
routes are recorded with an empty endpoint (contract §8), never with part of the address.
``fastify-plugin`` (``fp``) plugins share their parent's context, so a ``prefix`` given to them is
ignored — as Fastify does.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import (
    TsAliasIndex,
    _is_fixture_path,
    _module_of,
    _string_literal,
    relative_requires,
    require_specifier,
    resolve_const_value,
)
from ..typescript.imports import _resolve as _resolve_module
from ..typescript_express.mounts import mw_name
from ..typescript_express.routes import package_locals

FACT = "fastify.plugin-prefixes"

#: Anchors a scope's prefix is relative to.
ROOT: tuple[str, ...] = ("root",)  # an app instance: no prefix
LOOSE: tuple[str, ...] = ("loose",)  # a typed instance nobody registers here: see ``loose_base``
UNKNOWN: tuple[str, ...] = ("unknown",)  # e.g. a plugin registered twice in this file, differently

#: The module specifier of an edge into the registering file itself.
SELF = "\0self"

#: A prefix token meaning "whatever ``prefix`` the anchor plugin was itself registered with" —
#: an autoload ``options: { ...opts }`` that forwards the plugin's own options.
FROM_OPTS = "\0opts"

#: Packages whose default export (or ``fastify`` named export) makes an app instance.
_FACTORY_PACKAGES = frozenset({"fastify"})
_FP_PACKAGES = frozenset({"fastify-plugin"})
_AUTOLOAD_PACKAGES = frozenset({"@fastify/autoload", "fastify-autoload"})

#: Lifecycle hooks that run before the handler and can stop the request: their named functions
#: are guards. ``onSend`` / ``onResponse`` / ``onError`` run after — not guards.
GUARD_HOOKS = ("onRequest", "preParsing", "preValidation", "preHandler")

_FUNCTIONS = frozenset({
    "function_declaration", "function_expression", "arrow_function", "function",
    "generator_function_declaration",
})
_PLUGIN_TYPE = re.compile(r"\bFastifyPlugin\w*\b")
_INSTANCE_TYPE = re.compile(r"\bFastifyInstance\b")


@dataclass(slots=True)
class Scope:
    """One Fastify instance binding of a file."""

    #: The function the instance is the first parameter of, or the block an app variable is
    #: declared in; routes on ``name`` inside this node use this scope.
    node: Node
    name: str
    anchor: tuple[str, ...]
    #: Prefix added inside this file on top of the anchor's (``""`` none, ``None`` unresolved).
    local: str | None
    #: Guards of the enclosing scopes in this file that cover this one, outermost first.
    guards: tuple[str, ...] = ()
    #: The plugin function's options parameter (``opts``), if any.
    opts: str | None = None
    #: ``addHook`` guards registered on this instance: (byte offset, name).
    hooks: list[tuple[int, str]] = field(default_factory=list)


@dataclass(slots=True)
class Edge:
    """``R.register(target, { prefix })`` where ``target`` is another file's plugin."""

    anchor: tuple[str, ...]
    local: str | None
    guards: tuple[str, ...]
    spec: str  # relative module specifier
    export: str  # the name the target exports it as (``default``)
    prefix: str | None  # ``""`` none, ``None`` unresolved, or FROM_OPTS
    fp: bool  # wrapped with fastify-plugin at the registration
    site: str  # ``line``


@dataclass(slots=True)
class Autoload:
    """``R.register(AutoLoad, { dir, options, … })``."""

    anchor: tuple[str, ...]
    local: str | None
    guards: tuple[str, ...]
    #: The folder, as path segments relative to this file's folder; ``None`` = can't resolve.
    dir: tuple[str, ...] | None
    options: dict[str, Any]


@dataclass(slots=True)
class FileFastify:
    scopes: list[Scope] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    autoloads: list[Autoload] = field(default_factory=list)
    #: Exported plugin functions: export name → wrapped with fastify-plugin.
    exports: dict[str, bool] = field(default_factory=dict)
    #: ``autoPrefix`` / ``prefixOverride`` / ``autoConfig.prefix`` exports: name → value
    #: (``None`` = present but not a literal). Absent keys are not set.
    config: dict[str, str | None] = field(default_factory=dict)
    #: Named guard hooks the default-export plugin adds on its instance (an autohooks file).
    export_hooks: tuple[str, ...] = ()
    #: The file imports a Fastify package (fastify, fastify-plugin, @fastify/*).
    imports_fastify: bool = False


# --- small helpers ---------------------------------------------------------------------------


def join_prefix(*parts: str) -> str:
    """Join prefix pieces with single slashes; ``""`` when every piece is empty."""
    segs = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/" + "/".join(segs) if segs else ""


def _params(fn: Node) -> list[Node]:
    params = fn.child_by_field_name("parameters")
    if params is not None:
        return list(params.named_children)
    one = fn.child_by_field_name("parameter")  # x => …
    return [one] if one is not None else []


def _param_name(param: Node, source: bytes) -> str | None:
    if param.type == "identifier":
        return node_text(param, source)
    pat = param.child_by_field_name("pattern")
    return node_text(pat, source) if pat is not None and pat.type == "identifier" else None


def _param_typed(param: Node, source: bytes) -> bool:
    typ = param.child_by_field_name("type")
    return typ is not None and _INSTANCE_TYPE.search(node_text(typ, source)) is not None


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type in (
        "parenthesized_expression", "as_expression", "satisfies_expression", "await_expression",
    ):
        node = node.named_children[0] if node.named_children else None
    return node


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def _member_call(call: Node, source: bytes) -> tuple[str, str] | None:
    """``R.m(…)`` → (``R``, ``m``) for an identifier receiver; else None."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
    if obj is None or prop is None or obj.type != "identifier":
        return None
    return node_text(obj, source), node_text(prop, source)


def pair_value(obj: Node, key: str, source: bytes) -> Node | None:
    """The value of ``key`` in an object literal (the last one wins, as in JavaScript)."""
    found: Node | None = None
    for p in obj.named_children:
        if p.type == "pair":
            k = p.child_by_field_name("key")
            name = node_text(k, source).strip("'\"") if k is not None else None
            if name == key:
                found = p.child_by_field_name("value")
        elif p.type == "shorthand_property_identifier" and node_text(p, source) == key:
            found = p
    return found


def _has_spread(obj: Node) -> list[Node]:
    return [c.named_children[0] for c in obj.named_children
            if c.type == "spread_element" and c.named_children]


def guard_names(value: Node | None, source: bytes) -> list[str]:
    """A hook option's named functions: ``preHandler: auth`` / ``[a, b]`` /
    ``fastify.auth([…])``. Inline functions have no name and add nothing."""
    if value is None:
        return []
    items = value.named_children if value.type == "array" else [value]
    return [n for item in items if (n := mw_name(item, source)) is not None]


# --- one file --------------------------------------------------------------------------------


class _Analyzer:
    def __init__(self, root: Node, source: bytes, consts: dict[str, str | None] | None) -> None:
        self.root = root
        self.source = source
        self.consts = consts or {}
        packages = package_locals(root, source)
        self.factories = {n for n, (pkg, exp) in packages.items()
                          if pkg in _FACTORY_PACKAGES and exp in ("*", "fastify", "default")}
        self.fps = {n for n, (pkg, _) in packages.items() if pkg in _FP_PACKAGES}
        self.autoloads = {n for n, (pkg, _) in packages.items() if pkg in _AUTOLOAD_PACKAGES}
        self.out = FileFastify(imports_fastify=any(
            pkg == "fastify" or pkg.startswith(("fastify-", "@fastify/"))
            for pkg, _ in packages.values()))
        self.imports = self._imports()
        self.functions = self._functions()
        self.by_node: dict[int, Scope] = {}
        #: Function node id → the name the file exports it as (its plugin's key in the fact).
        self.exported: dict[int, str] = {}

    # Bindings ------------------------------------------------------------------------------

    def _imports(self) -> dict[str, tuple[str, str]]:
        """Imported name → (module specifier, the name the module exports it as). Relative and
        aliased (``@app/routes/users``) specifiers alike: the fact resolves them against the
        TypeScript index, and a package's (``@fastify/cors``) resolves to no file."""
        out: dict[str, tuple[str, str]] = {}
        for node in self.root.named_children:
            if node.type != "import_statement":
                continue
            module = _module_of(node, self.source)
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if module is None or clause is None:
                continue
            for c in clause.named_children:
                if c.type == "identifier":
                    out[node_text(c, self.source)] = (module, "default")
                elif c.type == "named_imports":
                    for spec in c.named_children:
                        ids = [x for x in spec.named_children if x.type == "identifier"]
                        if spec.type == "import_specifier" and ids:
                            out[node_text(ids[-1], self.source)] = (
                                module, node_text(ids[0], self.source))
        for name, imp in relative_requires(self.root, self.source).items():
            out.setdefault(name, imp)
        return out

    def _functions(self) -> dict[str, Node | None]:
        """Name → the function it is bound to in this file (a declaration, or a variable whose
        value is a function, possibly ``fp(…)``-wrapped); a name bound twice → None."""
        out: dict[str, Node | None] = {}
        stack = [self.root]
        while stack:
            n = stack.pop()
            name: Node | None = None
            if n.type in ("function_declaration", "generator_function_declaration"):
                name, fn = n.child_by_field_name("name"), n
            elif n.type == "variable_declarator":
                name, fn = n.child_by_field_name("name"), n
            else:
                fn = None
            if name is not None and name.type == "identifier" and fn is not None:
                key = node_text(name, self.source)
                out[key] = fn if key not in out else None
            stack.extend(n.named_children)
        return out

    def plugin_fn(self, node: Node | None, depth: int = 0) -> tuple[Node, bool, bool] | None:
        """A plugin expression → (its function, wrapped with fastify-plugin, typed as a Fastify
        plugin). Follows local names and ``fp(…)``."""
        node = _unwrap(node)
        if node is None or depth > 4:
            return None
        if node.type in _FUNCTIONS:
            params = _params(node)
            typed = bool(params) and _param_typed(params[0], self.source)
            return node, False, typed
        if node.type == "variable_declarator":
            typ = node.child_by_field_name("type")
            inner = self.plugin_fn(node.child_by_field_name("value"), depth + 1)
            if inner is None:
                return None
            typed = typ is not None and _PLUGIN_TYPE.search(node_text(typ, self.source)) is not None
            return inner[0], inner[1], inner[2] or typed
        if node.type == "identifier":
            target = self.functions.get(node_text(node, self.source))
            return self.plugin_fn(target, depth + 1) if target is not None else None
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = _args(node)
            if fn is not None and node_text(fn, self.source) in self.fps and args:
                inner = self.plugin_fn(args[0], depth + 1)
                return (inner[0], True, inner[2]) if inner is not None else None
        return None

    def _exported(self) -> list[tuple[str, Node]]:
        """(export name, the exported expression) for the file's exports."""
        out: list[tuple[str, Node]] = []
        src = self.source
        for node in self.root.named_children:
            if node.type == "export_statement":
                value = node.child_by_field_name("value") or node.child_by_field_name("declaration")
                is_default = any(c.type == "default" for c in node.children)
                if value is None:
                    continue
                if is_default:
                    out.append(("default", value))
                elif value.type in ("function_declaration", "generator_function_declaration"):
                    name = value.child_by_field_name("name")
                    if name is not None:
                        out.append((node_text(name, src), value))
                elif value.type in ("lexical_declaration", "variable_declaration"):
                    for d in value.named_children:
                        name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                        if name is not None and name.type == "identifier":
                            out.append((node_text(name, src), d))
            elif node.type == "expression_statement" and node.named_children:
                expr = node.named_children[0]
                if expr.type != "assignment_expression":
                    continue
                left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
                if left is None or right is None:
                    continue
                target = node_text(left, src)
                if target == "module.exports":
                    out.append(("default", right))
                elif target.startswith(("module.exports.", "exports.")) and target.count(".") <= 2:
                    out.append((target.rsplit(".", 1)[-1], right))
        return out

    def _config(self, exported: list[tuple[str, Node]], default_names: set[str]) -> None:
        """``autoPrefix`` / ``prefixOverride`` / ``autoConfig`` — exported, or set on the
        default-export plugin (``plugin.autoPrefix = '/x'``)."""
        src = self.source
        values: list[tuple[str, Node]] = list(exported)
        for node in self.root.named_children:
            expr = node.named_children[0] if node.type == "expression_statement" and node.named_children else None
            if expr is None or expr.type != "assignment_expression":
                continue
            left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
            if left is None or right is None or left.type != "member_expression":
                continue
            obj = left.child_by_field_name("object")
            prop = left.child_by_field_name("property")
            if obj is not None and prop is not None and node_text(obj, src) in default_names:
                values.append((node_text(prop, src), right))
        for name, raw in values:
            value = _unwrap(raw.child_by_field_name("value")
                            if raw.type == "variable_declarator" else raw)
            if value is None:
                continue
            if name in ("autoPrefix", "prefixOverride"):
                self.out.config[name] = _string_literal(value, src)
            elif name == "autoConfig":
                if value.type == "object":
                    p = pair_value(value, "prefix", src)
                    if p is not None:
                        self.out.config["autoConfig"] = _string_literal(_unwrap(p), src)
                else:  # a function of the instance: can't know
                    self.out.config["autoConfig"] = None

    # Scopes --------------------------------------------------------------------------------

    def _add_scope(self, fn: Node, anchor: tuple[str, ...], local: str | None,
                   guards: tuple[str, ...]) -> Scope | None:
        params = _params(fn)
        name = _param_name(params[0], self.source) if params else None
        if name is None:
            return None
        opts = _param_name(params[1], self.source) if len(params) > 1 else None
        old = self.by_node.get(fn.id)
        if old is not None:  # one function registered twice: one address only if both agree
            if (old.anchor, old.local) != (anchor, local):
                old.anchor, old.local = UNKNOWN, None
            return old
        scope = Scope(fn, name, anchor, local, guards, opts)
        self.by_node[fn.id] = scope
        self.out.scopes.append(scope)
        return scope

    def lookup(self, node: Node, name: str) -> Scope | None:
        """The scope ``name`` is bound in at ``node``: the nearest enclosing scope node whose
        instance is called ``name``. A nearer function that rebinds ``name`` as a parameter
        hides the outer one."""
        anc = node.parent
        while anc is not None:
            scope = self.by_node.get(anc.id)
            if scope is not None and scope.name == name:
                return scope
            if anc.type in _FUNCTIONS and any(
                    _param_name(p, self.source) == name for p in _params(anc)):
                return None  # shadowed by a parameter that is not a known instance
            for app in self.apps.get(anc.id, ()):
                if app.name == name:
                    return app
            anc = anc.parent
        return None

    def _apps(self) -> None:
        """``const app = Fastify(…)`` / ``require('fastify')(…)``: app instances."""
        self.apps: dict[int, list[Scope]] = {}
        stack = [self.root]
        while stack:
            n = stack.pop()
            if n.type == "variable_declarator":
                name, value = n.child_by_field_name("name"), _unwrap(n.child_by_field_name("value"))
                if name is not None and name.type == "identifier" and value is not None \
                        and value.type == "call_expression" and self._is_factory(value):
                    block = n.parent
                    while block is not None and block.type not in (*_FUNCTIONS, "program",
                                                                    "statement_block"):
                        block = block.parent
                    if block is not None:
                        scope = Scope(block, node_text(name, self.source), ROOT, "")
                        self.apps.setdefault(block.id, []).append(scope)
                        self.out.scopes.append(scope)
            stack.extend(n.named_children)

    def _is_factory(self, call: Node) -> bool:
        fn = _unwrap(call.child_by_field_name("function"))
        if fn is None:
            return False
        if fn.type == "identifier":
            return node_text(fn, self.source) in self.factories
        if fn.type == "member_expression":  # Fastify.fastify() / Fastify.default()
            obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
            return (obj is not None and prop is not None and node_text(obj, self.source) in self.factories
                    and node_text(prop, self.source) in ("fastify", "default"))
        if fn.type == "call_expression":  # require('fastify')(…)
            callee = fn.child_by_field_name("function")
            args = _args(fn)
            return (callee is not None and node_text(callee, self.source) == "require" and bool(args)
                    and _string_literal(args[0], self.source) == "fastify")
        return False

    # Registrations -------------------------------------------------------------------------

    def prefix_token(self, opts: Node | None, scope: Scope) -> str | None:
        """The ``prefix`` a registration's options give: ``""`` none, a literal, ``None`` when
        it can't be read, :data:`FROM_OPTS` for the enclosing plugin's own options."""
        opts = _unwrap(opts)
        if opts is None:
            return ""
        src = self.source
        if opts.type == "identifier":
            return FROM_OPTS if node_text(opts, src) == scope.opts and scope.local == "" else None
        if opts.type == "call_expression":  # Object.assign({}, opts)
            fn = opts.child_by_field_name("function")
            args = _args(opts)
            if fn is not None and node_text(fn, src) == "Object.assign" and args:
                merged = [a for a in args if not (a.type == "object" and not a.named_children)]
                if len(merged) == 1:
                    return self.prefix_token(merged[0], scope)
            return None
        if opts.type != "object":
            return None
        value = pair_value(opts, "prefix", src)
        if value is not None:
            value = _unwrap(value)
            if value is None:
                return None
            if value.type in ("string", "template_string"):
                return resolve_const_value(value, src, self.consts)
            return None
        spreads = _has_spread(opts)
        if not spreads:
            return ""
        if len(spreads) == 1 and spreads[0].type == "identifier" \
                and node_text(spreads[0], src) == scope.opts and scope.local == "":
            return FROM_OPTS
        return None

    def _register(self, call: Node, scope: Scope) -> None:
        args = _args(call)
        if not args:
            return
        src = self.source
        target = _unwrap(args[0])
        fp_site = False
        if target is not None and target.type == "call_expression":
            fn = target.child_by_field_name("function")
            inner = _args(target)
            if fn is not None and node_text(fn, src) in self.fps and inner:
                fp_site, target = True, _unwrap(inner[0])
        if target is None:
            return
        opts = args[1] if len(args) > 1 else None
        guards = self._guards_at(call, scope)
        if target.type == "identifier" and node_text(target, src) in self.autoloads:
            self.out.autoloads.append(self._autoload(opts, scope, guards))
            return
        self._link(call, scope, target, self.prefix_token(opts, scope), fp_site, guards)

    def _guards_at(self, call: Node, scope: Scope) -> tuple[str, ...]:
        return scope.guards + tuple(n for off, n in scope.hooks if off < call.start_byte)

    def _link(self, call: Node, scope: Scope, target: Node, prefix: str | None, fp_site: bool,
              guards: tuple[str, ...]) -> None:
        """``target`` runs on ``scope``'s instance (as a plugin, or called with it) under
        ``prefix``: another file's plugin → an :class:`Edge`; a function of this file → a scope."""
        src = self.source
        line = str(call.start_point[0] + 1)
        if target.type == "identifier" and node_text(target, src) in self.imports \
                and self.plugin_fn(target) is None:  # a function of this file wins
            spec, export = self.imports[node_text(target, src)]
            self.out.edges.append(Edge(scope.anchor, scope.local, guards, spec, export, prefix,
                                       fp_site, line))
            return
        required = require_specifier(target, src) or _dynamic_import(target, src)
        if required is not None:
            self.out.edges.append(Edge(scope.anchor, scope.local, guards, required, "default",
                                       prefix, fp_site, line))
            return
        plugin = self.plugin_fn(target)
        if plugin is None:
            return  # a package's plugin (cors, helmet …): no routes of ours
        fn, fp, _ = plugin
        own_export = self.exported.get(fn.id)
        if own_export is not None:
            # An exported plugin also registered here: one more registration of it, which the
            # fact weighs with the other files' (one address only if they all agree).
            self.out.edges.append(Edge(scope.anchor, scope.local, guards, SELF, own_export,
                                       prefix, fp_site, line))
            return
        if fp or fp_site:
            local = scope.local
        elif scope.local is None or prefix is None or prefix == FROM_OPTS:
            local = None
        else:
            local = join_prefix(scope.local, prefix)
        self._add_scope(fn, scope.anchor, local, guards)

    def _call_with_instance(self, call: Node) -> None:
        """``setupRoutes(app)`` / ``await registerUsers(server as X)``: a function of the repo
        called with an instance as its first argument runs on that instance — like ``register``
        without a prefix of its own."""
        fn = call.child_by_field_name("function")
        args = _args(call)
        if fn is None or fn.type != "identifier" or not args:
            return
        first = _unwrap(args[0])
        if first is None or first.type != "identifier":
            return
        name = node_text(fn, self.source)
        if name not in self.imports and name not in self.functions:
            return
        scope = self.lookup(call, node_text(first, self.source))
        if scope is not None:
            self._link(call, scope, fn, "", False, self._guards_at(call, scope))

    def _autoload(self, opts: Node | None, scope: Scope, guards: tuple[str, ...]) -> Autoload:
        src = self.source
        options: dict[str, Any] = {}
        directory: tuple[str, ...] | None = None
        opts = _unwrap(opts)
        if opts is None or opts.type != "object":
            return Autoload(scope.anchor, scope.local, guards, None, {})
        for c in opts.named_children:
            if c.type == "spread_element":
                options["unknown"] = True
        dir_node = pair_value(opts, "dir", src)
        directory = _dir_segments(_unwrap(dir_node), src)
        inner = pair_value(opts, "options", src)
        options["prefix"] = self.prefix_token(inner, scope) if inner is not None else ""
        for key in ("dirNameRoutePrefix", "autoHooks", "cascadeHooks", "routeParams",
                    "appendAutoPrefix", "encapsulate", "overwriteHooks"):
            v = _unwrap(pair_value(opts, key, src))
            if v is not None:
                options[key] = {"true": True, "false": False}.get(node_text(v, src), "unknown")
        for key in ("ignorePattern", "matchFilter", "ignoreFilter"):
            v = _unwrap(pair_value(opts, key, src))
            if v is not None:
                options[key] = _filter_value(v, src)
        v = _unwrap(pair_value(opts, "maxDepth", src))
        if v is not None:
            text = node_text(v, src)
            options["maxDepth"] = int(text) if text.isdigit() else "unknown"
        for key in ("indexPattern", "scriptPattern", "autoHooksPattern"):
            if pair_value(opts, key, src) is not None:
                options["unknown"] = True
        return Autoload(scope.anchor, scope.local, guards, directory, options)

    def _walk(self) -> None:
        """Registrations and hooks, in source order (an inline plugin's scope exists before its
        body is read)."""
        stack = [self.root]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                mc = _member_call(n, self.source)
                if mc is not None and mc[1] in ("register", "addHook"):
                    scope = self.lookup(n, mc[0])
                    if scope is not None:
                        if mc[1] == "register":
                            self._register(n, scope)
                        else:
                            self._hook(n, scope)
                elif mc is None:
                    self._call_with_instance(n)
            stack.extend(reversed(n.named_children))

    def _hook(self, call: Node, scope: Scope) -> None:
        args = _args(call)
        if len(args) >= 2 and _string_literal(args[0], self.source) in GUARD_HOOKS:
            name = mw_name(args[1], self.source)
            if name is not None:
                scope.hooks.append((call.start_byte, name))

    def run(self) -> FileFastify:
        self._apps()
        exported = self._exported()
        default_names: set[str] = set()
        for name, expr in exported:
            plugin = self.plugin_fn(expr)
            if plugin is None:
                continue
            fn, fp, typed = plugin
            self.out.exports[name] = fp
            bare = _unwrap(expr)
            if name == "default" and bare is not None and bare.type == "identifier":
                default_names.add(node_text(bare, self.source))
            # A Fastify type or fastify-plugin says it is a plugin; otherwise only a registration
            # (or autoload) the repo index finds does — importing fastify is not enough.
            evidence = typed or fp
            self.exported.setdefault(fn.id, name)
            scope = self._add_scope(fn, ("plugin", name), "", ())
            if scope is not None and not evidence:
                scope.anchor = ("plugin?", name)  # an instance only if the repo registers it
        self._config(exported, default_names)
        # Typed instance parameters nobody here registers or exports.
        stack = [self.root]
        while stack:
            n = stack.pop()
            if n.type in _FUNCTIONS and n.id not in self.by_node:
                params = _params(n)
                if params and _param_typed(params[0], self.source):
                    self._add_scope(n, LOOSE, "", ())
            elif n.type == "variable_declarator":
                typ = n.child_by_field_name("type")
                value = _unwrap(n.child_by_field_name("value"))
                if typ is not None and _PLUGIN_TYPE.search(node_text(typ, self.source)) \
                        and value is not None and value.type in _FUNCTIONS \
                        and value.id not in self.by_node:
                    self._add_scope(value, LOOSE, "", ())
            stack.extend(n.named_children)
        self._walk()
        default = next((s for s in self.out.scopes if s.anchor[-1:] == ("default",)), None)
        if default is not None:
            self.out.export_hooks = tuple(name for _, name in default.hooks)
        return self.out


def _dynamic_import(node: Node, source: bytes) -> str | None:
    """``import('./x')`` → ``./x``."""
    if node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    args = _args(node)
    if fn is None or fn.type != "import" or not args:
        return None
    spec = _string_literal(args[0], source)
    return spec if spec is not None and spec.startswith(".") else None


_HERE = ("__dirname", "import.meta.dirname", "import.meta.url")


def _dir_segments(node: Node | None, source: bytes) -> tuple[str, ...] | None:
    """An autoload ``dir`` → its path relative to this file's folder, as segments:
    ``path.join(__dirname, 'routes')`` / ``join(import.meta.url, 'routes')`` (desm) /
    ``path.join(path.dirname(fileURLToPath(import.meta.url)), 'routes')`` /
    ``fileURLToPath(new URL('./routes', import.meta.url))``. Anything else → None."""
    if node is None:
        return None
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        args = _args(node)
        name = node_text(fn, source).rsplit(".", 1)[-1] if fn is not None else ""
        if name == "fileURLToPath" and len(args) == 1:
            return _dir_segments(_unwrap(args[0]), source)
        if name in ("join", "resolve") and args and _is_here(args[0], source):
            parts = [_string_literal(a, source) for a in args[1:]]
            if any(p is None for p in parts):
                return None
            return tuple(s for p in parts for s in p.split("/") if s and s != ".")  # type: ignore[union-attr]
        return None
    if node.type == "new_expression":
        ctor = node.child_by_field_name("constructor")
        args = _args(node)
        if ctor is not None and node_text(ctor, source) == "URL" and len(args) == 2 \
                and node_text(args[1], source) == "import.meta.url":
            spec = _string_literal(args[0], source)
            return tuple(s for s in spec.split("/") if s and s != ".") if spec is not None else None
    return None


def _is_here(node: Node, source: bytes) -> bool:
    """This file's folder: ``__dirname``, ``import.meta.dirname`` / ``.url``, or
    ``dirname(fileURLToPath(import.meta.url))``."""
    text = node_text(node, source)
    if text in _HERE:
        return True
    return node.type == "call_expression" and text.replace(" ", "") in (
        "path.dirname(fileURLToPath(import.meta.url))", "dirname(fileURLToPath(import.meta.url))",
        "path.dirname(url.fileURLToPath(import.meta.url))",
    )


def _filter_value(node: Node, source: bytes) -> tuple[str, str] | str:
    """An autoload filter option → ``("re", pattern)`` / ``("re-i", …)`` / ``("str", text)``;
    ``"unknown"`` for a function or a variable."""
    if node.type == "regex":
        pattern = node.child_by_field_name("pattern")
        flags = node.child_by_field_name("flags")
        kind = "re-i" if flags is not None and "i" in node_text(flags, source) else "re"
        return (kind, node_text(pattern, source)) if pattern is not None else "unknown"
    text = _string_literal(node, source)
    return ("str", text) if text is not None else "unknown"


def analyze(root: Node, source: bytes, consts: dict[str, str | None] | None = None) -> FileFastify:
    """The Fastify instances, registrations and plugin exports of one file."""
    return _Analyzer(root, source, consts).run()


def analyzer(root: Node, source: bytes, consts: dict[str, str | None] | None = None) -> _Analyzer:
    """:func:`analyze`, keeping the analyzer for :meth:`_Analyzer.lookup` (which scope a call's
    receiver is bound in)."""
    a = _Analyzer(root, source, consts)
    a.run()
    return a


# --- the repo-wide fact ----------------------------------------------------------------------


@dataclass(frozen=True)
class PluginPrefixes:
    """``bases``: (file, export name) → the prefix that plugin's routes are served under
    (``""`` none), ``None`` when it can't be resolved (registered under two prefixes, or one that
    can't be read). ``guards``: the same keys → guards that certainly cover it (hooks of the
    registering plugins, ``autohooks`` files). ``loose_unknown``: some autoload folder couldn't
    be resolved — a plugin nobody visibly registers may be loaded from it, under an unknown
    prefix."""

    bases: dict[tuple[str, str], str | None] = field(default_factory=dict)
    guards: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    loose_unknown: bool = False
    #: Files holding a plugin in ``bases`` (the detector runs on them whatever they import).
    files: frozenset[str] = frozenset()


def plugin_prefixes(index: Any | None) -> PluginPrefixes:
    return index_fact(index, FACT) or PluginPrefixes()


_GATE = (b"fastify", b"Fastify", b"autoPrefix", b"prefixOverride", b"autoConfig", b".register(")


@dataclass(slots=True)
class _FileValue:
    rel: str
    repo_root: str
    edges: list[tuple[tuple[str, ...], str | None, tuple[str, ...], str, str, str | None, bool]]
    autoloads: list[tuple[tuple[str, ...], str | None, tuple[str, ...], str | None, dict[str, Any]]]
    exports: dict[str, bool]
    config: dict[str, str | None]
    export_hooks: tuple[str, ...]


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileValue | None:
    if root is None or _is_fixture_path(rel):
        return None
    info = analyze(root, source)
    if not (info.edges or info.autoloads or info.exports or info.config):
        return None
    repo = repo_root or Path(".")
    here = (repo / rel).parent
    edges = [(e.anchor, e.local, e.guards, e.spec, e.export, e.prefix, e.fp) for e in info.edges]
    autoloads = []
    for a in info.autoloads:
        directory: str | None = None
        if a.dir is not None:
            rel_dir = os.path.relpath(os.path.normpath(here.joinpath(*a.dir)), repo)
            directory = None if rel_dir.startswith("..") else Path(rel_dir).as_posix()
        autoloads.append((a.anchor, a.local, a.guards, directory, a.options))
    return _FileValue(rel, str(repo), edges, autoloads, dict(info.exports), dict(info.config),
                      info.export_hooks)


_SCRIPT = re.compile(r"(?:(?:^.?|\.[^d]|[^.]d|[^.][^d])\.ts|\.js|\.cjs|\.mjs|\.cts|\.mts)$", re.I)
_INDEX = re.compile(r"^index(?:\.ts|\.js|\.cjs|\.mjs|\.cts|\.mts)$", re.I)
_AUTOHOOKS = re.compile(r"^[_.]?auto_?hooks(?:\.ts|\.js|\.cjs|\.mjs|\.cts|\.mts)$", re.I)


def _matches(filt: Any, path: str) -> bool | None:
    """An autoload filter against a path; None when it can't be evaluated."""
    if not isinstance(filt, tuple):
        return None
    kind, text = filt
    if kind == "str":
        return text in path
    try:
        return re.search(text, path, re.I if kind == "re-i" else 0) is not None
    except re.error:
        return None


def _autoload_files(
    root_dir: Path, opts: dict[str, Any], repo: Path
) -> list[tuple[str, str | None, list[str]]] | None:
    """The plugin files ``@fastify/autoload`` loads from ``root_dir``, as (repo-relative file,
    the folder prefix ``None`` for a top-level file, the autohooks files that cover it) — the
    same walk as ``@fastify/autoload``'s ``find-plugins.js``. None when an option makes the walk
    unknowable."""
    if not root_dir.is_dir():
        return None
    name_prefix = opts.get("dirNameRoutePrefix", True)
    auto_hooks = opts.get("autoHooks", False) is True
    cascade = opts.get("cascadeHooks", False) is True
    max_depth = opts.get("maxDepth")
    ignore = opts.get("ignorePattern")
    match_filter, ignore_filter = opts.get("matchFilter"), opts.get("ignoreFilter")
    if "unknown" in (name_prefix, max_depth, ignore, match_filter, ignore_filter):
        return None
    out: list[tuple[str, str | None, list[str]]] = []
    unknown = False

    def accumulate(file: Path, prefix: str | None, hooks: list[str]) -> None:
        nonlocal unknown
        path = "/" + file.relative_to(root_dir).as_posix()
        for filt, keep in ((match_filter, True), (ignore_filter, False)):
            if filt is None:
                continue
            hit = _matches(filt, path)
            if hit is None:
                unknown = True
                return
            if hit != keep:
                return
        out.append((file.relative_to(repo).as_posix(), prefix, hooks))

    def build(d: Path, prefix: str | None, depth: int, hooks: list[str]) -> None:
        nonlocal unknown
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            unknown = True
            return
        cur = list(hooks)
        if auto_hooks:
            hook = next((e for e in entries if _AUTOHOOKS.match(e.name)), None)
            if hook is not None:
                if opts.get("overwriteHooks") is True:
                    cur = []
                cur.append(hook.relative_to(repo).as_posix())
        index = next((e for e in entries if _INDEX.match(e.name)), None)
        if index is not None:
            accumulate(index, prefix, cur)
            if not any(e.is_dir() for e in entries):
                return
        for e in entries:
            if e.name == "node_modules":
                continue
            if ignore is not None:
                hit = _matches(ignore, e.relative_to(root_dir).as_posix())
                if hit is None:
                    unknown = True
                    return
                if hit:
                    continue
            at_max = isinstance(max_depth, int) and max_depth <= depth
            if e.is_dir() and not at_max:
                crumb = (f"{prefix}/" if prefix else "/") + (e.name if name_prefix is True else "")
                build(e, crumb, depth + 1, cur if auto_hooks and cascade else [])
            elif index is not None:
                continue
            elif e.is_file() and _SCRIPT.search(e.name) and not _AUTOHOOKS.match(e.name):
                accumulate(e, prefix, cur)

    build(root_dir, None, 0, [])
    return None if unknown else out


def _plugin_prefix(
    dir_prefix: str | None, config: dict[str, str | None], opts: dict[str, Any],
) -> tuple[bool, str | None]:
    """The ``prefix`` autoload registers one file's plugin with (its ``handlePrefixConfig``), as
    (appended to the autoload ``options.prefix``, the rest). ``prefixOverride`` replaces it all."""
    if "prefixOverride" in config:
        return False, config["prefixOverride"]
    auto = config["autoPrefix"] if "autoPrefix" in config else "\0"
    if auto is None:
        return True, None
    if opts.get("appendAutoPrefix") is True and auto != "\0" and dir_prefix:
        prefix: str | None = f"{dir_prefix}/{auto}"
    elif auto != "\0":
        prefix = auto
    else:
        prefix = dir_prefix
    if not prefix:
        return True, ""
    prefix = re.sub(r"/+", "/", prefix)
    if opts.get("routeParams") is True:
        prefix = prefix.replace("__", ":") if "__" in prefix else prefix.replace("/_", "/:")
    return True, prefix


def _with_options(options_prefix: str | None, appended: bool, rest: str | None) -> str | None:
    if not appended:
        return rest
    if options_prefix is None or rest is None:
        return None
    return (options_prefix[:-1] if options_prefix.endswith("/") else options_prefix) + rest


_Key = tuple[str, str]
#: An edge into a plugin: (registering file, its anchor, local prefix, guards, prefix, fp). The
#: prefix may be ``(FROM_OPTS, rest)``: an autoload ``options: { ...opts }``, resolved once the
#: registering plugin's own prefix is known.
_In = tuple[str, tuple[str, ...], str | None, tuple[str, ...], Any, bool]


def _resolve(values: list[_FileValue], index: object) -> PluginPrefixes | None:
    files = {v.rel: v for v in values}
    incoming: dict[_Key, list[_In]] = {}
    unknown_autoload = False
    skipped: set[_Key] = set()
    aliases = index if isinstance(index, TsAliasIndex) else None
    for v in values:
        for anchor, local, guards, spec, export, prefix, fp in v.edges:
            target = v.rel if spec == SELF else _resolve_module(spec, v.rel, Path(v.repo_root), aliases)
            if target is not None:
                incoming.setdefault((target, export), []).append(
                    (v.rel, anchor, local, guards, prefix, fp))
        for anchor, local, guards, directory, opts in v.autoloads:
            if directory is None or opts.get("unknown") or "unknown" in (
                    opts.get("encapsulate"), opts.get("routeParams"), opts.get("appendAutoPrefix")):
                unknown_autoload = True
                continue
            repo = Path(v.repo_root)
            loaded = _autoload_files(repo / directory, opts, repo)
            if loaded is None:
                unknown_autoload = True
                continue
            loaded_set = {f for f, _, _ in loaded}
            for p in (repo / directory).rglob("*"):
                rel = p.relative_to(repo).as_posix()
                if p.is_file() and _SCRIPT.search(p.name) and rel not in loaded_set:
                    skipped.add((rel, "default"))
            for file, dir_prefix, hooks in loaded:
                info = files.get(file)
                config = info.config if info is not None else {}
                fp = (info.exports.get("default", False) if info is not None else False) \
                    or opts.get("encapsulate") is False
                appended, rest = _plugin_prefix(dir_prefix, config, opts)
                options_prefix = opts.get("prefix", "")
                served: Any = (FROM_OPTS, rest) if appended and options_prefix == FROM_OPTS \
                    else _with_options(options_prefix, appended, rest)
                hook_guards = tuple(g for h in hooks if (hi := files.get(h)) is not None
                                    for g in hi.export_hooks)
                incoming.setdefault((file, "default"), []).append(
                    (v.rel, anchor, local, guards + hook_guards, served, fp))

    bases: dict[_Key, str | None] = {}
    guards_out: dict[_Key, tuple[str, ...] | None] = {}

    def is_fp(key: _Key) -> bool:
        info = files.get(key[0])
        return bool(info and info.exports.get(key[1]))

    def own_prefix(key: _Key) -> str | None:
        """The ``prefix`` option a plugin was registered with (for ``{ ...opts }``)."""
        found: dict[str, str | None] = {}
        for *_, prefix, _ in incoming.get(key, ()):
            record_distinct(found, "p", prefix if isinstance(prefix, str) and prefix != FROM_OPTS
                            else None)
        return found.get("p", "")

    def anchor_base(file: str, anchor: tuple[str, ...], seen: frozenset[_Key]
                    ) -> tuple[str | None, tuple[str, ...]]:
        if anchor == ROOT:
            return "", ()
        if anchor[0] in ("plugin", "plugin?"):
            return base_of((file, anchor[1]), seen)
        return None, ()

    def base_of(key: _Key, seen: frozenset[_Key]) -> tuple[str | None, tuple[str, ...]]:
        if key in seen:
            return None, ()
        if key in bases:
            return bases[key], guards_out.get(key) or ()
        edges = incoming.get(key)
        if not edges:
            if key in skipped:  # in an autoload folder, but autoload doesn't load it
                bases[key] = None
                return None, ()
            return (None if unknown_autoload else ""), ()
        found: dict[str, str | None] = {}
        found_guards: dict[str, tuple[str, ...] | None] = {}
        for src_file, anchor, local, guards, prefix, fp in edges:
            base, outer = anchor_base(src_file, anchor, seen | {key})
            ignored = fp or is_fp(key)  # fastify-plugin: the parent's context, no prefix
            if prefix == FROM_OPTS or isinstance(prefix, tuple):
                own = own_prefix((src_file, anchor[1])) \
                    if anchor[0] in ("plugin", "plugin?") else ""
                prefix = own if not isinstance(prefix, tuple) else _with_options(own, True, prefix[1])
            value = None if base is None or local is None or (prefix is None and not ignored) \
                else join_prefix(base, local, "" if ignored else prefix or "")
            record_distinct(found, "b", value)
            record_distinct(found_guards, "g", outer + guards)
        bases[key] = found.get("b")
        guards_out[key] = found_guards.get("g")
        return bases[key], guards_out[key] or ()

    for key in sorted(set(incoming) | skipped):
        base_of(key, frozenset())
    if not bases and not unknown_autoload:
        return None
    return PluginPrefixes(
        bases=dict(bases),
        guards={k: g for k, g in guards_out.items() if g},
        loose_unknown=unknown_autoload,
        files=frozenset(f for f, _ in bases),
    )


register_fact(FactSpec(name=FACT, language="typescript", gate=_GATE,
                       collect=_collect, resolve=_resolve))
