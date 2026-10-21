"""Express mounts as a registered fact, ``express.router-mounts`` (see ``parsers/additive.py``).

A mount is wiring: it gives every route of a router its prefix, from another file::

    // src/app.js
    app.use('/users', usersRouter());           // a router factory, defined in routes/users.js
    app.use('/matches', matchRouter);           // a router instance, exported by routes/matches.js
    // src/routes/matches.js
    export const matchRouter = Router();
    matchRouter.get('/:id', getMatch);           // → GET /matches/:id

While the TypeScript index is built, ``_collect`` reads each file's mounts (as :class:`Wiring`
prefixes) and the routers it exports; ``_resolve`` confirms router-instance mounts against the
target file's exports and keeps one base per router (:func:`~..wiring.resolve_wiring`: mounted at
two bases → dropped, the routes keep their own path). The Express detector reads the result with
:func:`express_mounts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import (
    _is_fixture_path,
    _module_of,
    _relative_import_map,
    _try_paths,
    _string_literal,
    relative_requires,
    require_specifier,
)
from ..wiring import Wiring, resolve_wiring

FACT = "express.router-mounts"

#: An Express router-instance mount candidate: (mount site ``file:line``, target file, the name
#: the target exports, base, the mount's own middleware). Confirmed against the target's router
#: exports in ``_resolve``.
_InstanceMount = tuple[str, str, str, str, tuple[str, ...]]
#: A router-factory mount: its prefix, and the mount's own middleware.
_FactoryMount = tuple[Wiring, tuple[str, ...]]


def _is_router_call(node: Node | None, source: bytes) -> bool:
    """``Router(...)`` / ``express.Router(...)`` (optionally ``new``) — an Express router instance."""
    if node is not None and node.type == "new_expression":
        fn = node.child_by_field_name("constructor")
    elif node is not None and node.type == "call_expression":
        fn = node.child_by_field_name("function")
    else:
        return False
    return fn is not None and node_text(fn, source) in {"Router", "express.Router"}


def router_locals(root: Node, source: bytes) -> set[str]:
    """Variables bound to an Express router instance anywhere in the file
    (``const r = Router()`` / ``express.Router()``)."""
    out: set[str] = set()

    def walk(node: Node) -> None:
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if (name is not None and name.type == "identifier"
                    and _is_router_call(node.child_by_field_name("value"), source)):
                out.add(node_text(name, source))
        for c in node.named_children:
            walk(c)

    walk(root)
    return out


@dataclass(frozen=True)
class ExpressMounts:
    """``factories``: a mounted router-factory's defining file → the base it is served under
    (``app.use('/users', usersRouter())`` → ``routes/users.js`` → ``/users``).
    ``routers``: a router file → {the router's local variable → its base}, only when the target
    file confirms the import is a ``Router()`` it exports (a middleware import is never a
    prefix). Test / fixture files are not a mount source.

    ``factory_guards`` / ``router_guards``: the same keys → the middleware the mount itself puts
    in front of the router (``app.use('/users', requireAuth, usersRouter)``) — they certainly run
    before every route of that router, so they are its routes' outermost guards. A router
    mounted twice with different middleware has none here."""

    factories: dict[str, str] = field(default_factory=dict)
    routers: dict[str, dict[str, str]] = field(default_factory=dict)
    factory_guards: dict[str, tuple[str, ...]] = field(default_factory=dict)
    router_guards: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    #: The mount sites (``file:line``) whose router is confirmed and keeps their base: its
    #: routes are recorded at the mount's prefix, so the mount itself is wiring, nothing more.
    served: frozenset[str] = frozenset()


def mw_name(node: Node, source: bytes) -> str | None:
    """A middleware/handler arg's callable name — ``requireAuth`` from ``requireAuth()``,
    ``Auth.check`` from the member call, ``rateLimit`` from a bare reference. An inline
    (arrow/function) middleware has no name → None (honest-null, no fabricated name)."""
    n = node.child_by_field_name("function") if node.type == "call_expression" else node
    if n is not None and n.type in ("identifier", "member_expression"):
        return node_text(n, source)
    return None


def _names(nodes: list[Node], source: bytes) -> tuple[str, ...]:
    return tuple(nm for n in nodes if (nm := mw_name(n, source)) is not None)


def express_mounts(index: Any | None) -> ExpressMounts:
    """The repo's Express mounts (empty without an index, e.g. single-file parsing)."""
    return index_fact(index, FACT) or ExpressMounts()


def _mount_receiver_ok(obj_text: str) -> bool:
    """Whether a ``X.use(...)`` receiver looks like an Express app/router — mirrors the runtime
    detector's ``_is_router_obj`` heuristic (duplicated to keep the framework parser out of the
    index module's imports)."""
    tail = obj_text.lower().rsplit(".", 1)[-1].strip()
    return tail in {"app", "router", "server", "api", "route"} or tail.endswith(("router", "app"))


def _factory_call_name(arg: Node, source: bytes) -> str | None:
    """If ``arg`` is a bare ``factory()`` call, its callee identifier name; else None. Member
    calls (``Auth.check()``) and non-calls are skipped."""
    if arg.type != "call_expression":
        return None
    fn = arg.child_by_field_name("function")
    return node_text(fn, source) if fn is not None and fn.type == "identifier" else None


def _relative_import_exports(root: Node, source: bytes) -> dict[str, tuple[str, str]]:
    """local imported name → (relative module specifier, the name the module exports it as):
    ``import x from './a'`` → ``x: ('./a', 'default')``; ``import { r as s } from './a'`` →
    ``s: ('./a', 'r')``. Namespace imports and non-relative specifiers are omitted."""
    out: dict[str, tuple[str, str]] = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        module = _module_of(node, source)
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if module is None or not module.startswith(".") or clause is None:
            continue
        for c in clause.named_children:
            if c.type == "identifier":
                out[node_text(c, source)] = (module, "default")
            elif c.type == "named_imports":
                for spec in c.named_children:
                    idents = [x for x in spec.named_children if x.type == "identifier"]
                    if spec.type == "import_specifier" and idents:
                        out[node_text(idents[-1], source)] = (module, node_text(idents[0], source))
    for nm, imp in relative_requires(root, source).items():
        out.setdefault(nm, imp)
    return out


def _collect_router_exports(root: Node, source: bytes) -> dict[str, str]:
    """Exported name → local variable, for each top-level router instance this file exports:
    ``export const r = Router()`` (``r → r``), ``export default r`` (``default → r``),
    ``export { r as s }`` (``s → r``). Only variables bound to ``Router()`` count."""
    if b"Router" not in source:
        return {}
    routers = router_locals(root, source)
    out: dict[str, str] = {}
    for node in root.named_children:
        if node.type != "export_statement" or not routers:
            continue
        for c in node.named_children:
            if c.type == "lexical_declaration":
                for d in c.named_children:
                    name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                    if name is not None and node_text(name, source) in routers:
                        out[node_text(name, source)] = node_text(name, source)
            elif c.type == "identifier" and node_text(c, source) in routers:  # export default r
                out["default"] = node_text(c, source)
            elif c.type == "export_clause":
                for spec in c.named_children:
                    idents = [x for x in spec.named_children if x.type == "identifier"]
                    if spec.type == "export_specifier" and idents and node_text(idents[0], source) in routers:
                        out[node_text(idents[-1], source)] = node_text(idents[0], source)
    for node in root.named_children:  # CommonJS: module.exports = r / exports.x = r / { r }
        expr = node.named_children[0] if node.type == "expression_statement" and node.named_children else None
        if expr is None or expr.type != "assignment_expression" or not routers:
            continue
        left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
        target = node_text(left, source) if left is not None else ""
        if right is None:
            continue
        if target == "module.exports":
            if right.type == "identifier" and node_text(right, source) in routers:
                out["default"] = node_text(right, source)
            elif right.type == "object":
                for p in right.named_children:
                    if p.type == "shorthand_property_identifier" and node_text(p, source) in routers:
                        out[node_text(p, source)] = node_text(p, source)
                    elif p.type == "pair":
                        key, value = p.child_by_field_name("key"), p.child_by_field_name("value")
                        if (key is not None and value is not None and value.type == "identifier"
                                and node_text(value, source) in routers):
                            out[node_text(key, source)] = node_text(value, source)
        elif target.startswith(("module.exports.", "exports.")) and right.type == "identifier" \
                and node_text(right, source) in routers:
            out[target.rsplit(".", 1)[-1]] = node_text(right, source)
    return out


def _distinct_guards(items: Any) -> dict[Any, tuple[str, ...]]:
    """Key → its mount middleware, one value per key (two different lists → none); empty lists
    left out."""
    seen: dict[Any, tuple[str, ...] | None] = {}
    for key, guards in items:
        record_distinct(seen, key, guards)
    return {k: v for k, v in seen.items() if v}


def _confirm_instance_mounts(
    candidates: list[_InstanceMount], router_exports: dict[str, dict[str, str]]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, tuple[str, ...]]], set[str]]:
    """Keep the router-instance mounts the target file confirms: the imported name is a
    ``Router()`` that file exports. A mount site with more than one confirmed router is skipped,
    and a router mounted at >1 distinct base is dropped (honest-null: its routes keep their own
    bare path)."""
    confirmed: dict[str, list[tuple[str, str, str, tuple[str, ...]]]] = {}
    for site, target, exported, base, guards in candidates:
        router_var = router_exports.get(target, {}).get(exported)
        if router_var is not None:
            confirmed.setdefault(site, []).append((target, router_var, base, guards))
    hits = [(site, hit) for site, found in confirmed.items() if len(found) == 1 for hit in found]
    wiring = [Wiring((target, local), "prefix", base, site)
              for site, (target, local, base, _) in hits]
    out: dict[str, dict[str, str]] = {}
    for (target, router_var), base in resolve_wiring(wiring).items():
        out.setdefault(target, {})[router_var] = base
    served = {site for site, (target, local, _, _) in hits if local in out.get(target, {})}
    guards_out: dict[str, dict[str, tuple[str, ...]]] = {}
    for (target, router_var), guards in _distinct_guards(
        ((target, local), guards) for _, (target, local, _, guards) in hits
    ).items():
        guards_out.setdefault(target, {})[router_var] = guards
    return out, guards_out, served


def _collect_express_mounts(
    root: Node, source: bytes, rel: str, repo_root: Path
) -> tuple[list[_FactoryMount], list[_InstanceMount]]:
    """Express mounts in this file: a prefix :class:`Wiring` scoped to the mounted factory's file
    (with the mount's own middleware) for each ``app.use('/base', factory())``, and router-instance candidates for each
    ``app.use('/base', importedName)``. The identifier is resolved to its defining file via this
    file's relative imports. A mount whose base is not a plain string, or with more than one
    imported factory, is skipped — honest-null, never a guessed link. Instance candidates are only
    candidates: the reduce keeps one when the target file exports that name as a ``Router()``.
    Test/fixture files are not a mount source (their throwaway bases must not define a router's
    real prefix)."""
    if b".use(" not in source or _is_fixture_path(rel):
        return [], []
    import_map = _relative_import_map(root, source)
    if not import_map and b"require(" not in source:
        return [], []
    exports_of = _relative_import_exports(root, source)
    mount_dir = (repo_root / rel).parent
    out: list[_FactoryMount] = []
    instances: list[_InstanceMount] = []

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
                obj = fn.child_by_field_name("object")
                if (prop is not None and node_text(prop, source) == "use"
                        and obj is not None and _mount_receiver_ok(node_text(obj, source))):
                    args = node.child_by_field_name("arguments")
                    arg_nodes = list(args.named_children) if args is not None else []
                    base = _string_literal(arg_nodes[0], source) if arg_nodes else None
                    if base is not None and base.startswith("/"):
                        factories = [
                            (a, import_map[name])
                            for a in arg_nodes[1:]
                            if (name := _factory_call_name(a, source)) is not None
                            and name in import_map
                        ] + [  # require('./r')() — an inline required factory
                            (a, spec) for a in arg_nodes[1:] if a.type == "call_expression"
                            and (spec := require_specifier(a.child_by_field_name("function"), source))
                        ]
                        specs = [spec for _, spec in factories]
                        if len(specs) == 1:  # exactly one imported factory arg → unambiguous
                            resolved = _try_paths(mount_dir / specs[0], repo_root)
                            if resolved is not None:
                                site = f"{rel}:{node.start_point[0] + 1}"
                                guards = _names([a for a in arg_nodes[1:]
                                                 if a is not factories[0][0]], source)
                                out.append((Wiring((resolved,), "prefix", base, site), guards))
                        elif not specs:  # router instances: every imported identifier is a candidate
                            site = f"{rel}:{node.start_point[0] + 1}"
                            for a in arg_nodes[1:]:
                                inline = require_specifier(a, source)  # app.use('/x', require('./r'))
                                imp = ((inline, "default") if inline is not None else
                                       exports_of.get(node_text(a, source)) if a.type == "identifier" else None)
                                target = _try_paths(mount_dir / imp[0], repo_root) if imp else None
                                if imp is not None and target is not None:
                                    guards = _names([b for b in arg_nodes[1:] if b is not a], source)
                                    instances.append((site, target, imp[1], base, guards))
        for c in node.named_children:
            walk(c)

    walk(root)
    return out, instances



_FileMounts = tuple[list[_FactoryMount], list[_InstanceMount], tuple[str, dict[str, str]]]


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileMounts | None:
    factories, instances = _collect_express_mounts(root, source, rel, repo_root or Path("."))
    exports = _collect_router_exports(root, source)
    if not factories and not instances and not exports:
        return None
    return factories, instances, (rel, exports)


def _resolve(values: list[_FileMounts], index: object) -> ExpressMounts | None:
    factories: list[_FactoryMount] = []
    candidates: list[_InstanceMount] = []
    router_exports: dict[str, dict[str, str]] = {}  # file → exported name → local router variable
    for file_factories, file_instances, (rel, exports) in values:
        factories.extend(file_factories)
        candidates.extend(file_instances)
        if exports:
            router_exports[rel] = exports
    routers, router_guards, served = _confirm_instance_mounts(candidates, router_exports)
    factory_bases = {scope[0]: base
                     for scope, base in resolve_wiring(w for w, _ in factories).items()}
    served |= {w.site for w, _ in factories if w.scope[0] in factory_bases}
    mounts = ExpressMounts(
        factories=factory_bases,
        routers=routers,
        factory_guards=_distinct_guards((w.scope[0], g) for w, g in factories),
        router_guards=router_guards,
        served=frozenset(served),
    )
    return mounts if mounts.factories or mounts.routers else None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b".use(", b"Router"),
    collect=_collect, resolve=_resolve,
))
