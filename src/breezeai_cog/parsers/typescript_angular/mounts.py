"""Angular lazy-route mounts as a registered fact on the TypeScript index,
``angular.route-mounts``.

A lazy child's routes are served under the path its parent mounts it at, in another file. The
child is reached either through a class (an NgModule, ``.then(m => m.X)``) or, for a standalone
default-export routes array, through the file the import loads::

    // app-routing.module.ts
    { path: 'company/:brandId', loadChildren: () => import('./brand/brand.module')
        .then(m => m.BrandModule) }
    // brand.module.ts:  @NgModule({ imports: [BrandRoutingModule] }) export class BrandModule {}

    // app.routes.ts
    { path: 'admin', loadChildren: () => import('./admin/admin.routes') }
    // admin/admin.routes.ts:  export default [ { path: 'docs', … } ]   → /admin/docs

The fact maps each target, ``("symbol", "BrandModule")`` or ``("file", "src/app/admin/admin.routes.ts")``,
to its **fully composed** mount prefix (the parent chain prepended, ``NgModule`` imports
propagated), so a child parsed in its own file starts at that prefix instead of ``""``. A target
mounted at more than one prefix, or reached through an unresolved path, maps to ``None``: its
routes' full address is unknown, so they get a null endpoint (honest-null), never their bare path.
Paths may name constants of other files (``RouteNames.X``), so they resolve against the index's
constant map.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import (
    _CLASS_NODES, _join_segments, _path_pieces, _resolve_pieces, _try_paths,
)

FACT = "angular.route-mounts"

#: A lazy mount's target: ``("symbol", "AdminModule")`` or ``("file", "src/app/admin/x.ts")``.
Target = tuple[str, str]

# A path expression is returned as UNRESOLVED pieces — ``("lit", text)`` for a string literal
# and ``("sym", "RouteNames.X")`` for a const reference — because ``RouteNames.*`` is defined
# cross-file, so the pieces are resolved against the repo-wide ``const_values`` in the reduce
# (:func:`~..typescript.imports._resolve_pieces`), not here.
_MountRaw = tuple[list[Target], list[tuple[str, str]] | None]  # (targets, path pieces)

# `.then(m => m.OrganisationsRoutingModule)` — the mounted module's exported class symbol.
_MOUNT_TARGET_RE = re.compile(r"=>\s*[\w$]+\.([A-Za-z_$][\w$]*)")
# `import('./admin/admin.routes')` — the loaded file.
_MOUNT_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]""")


def _targets(load: Node, source: bytes, rel: str, repo_root: Path | None) -> list[Target]:
    """What a ``loadChildren`` loads: the ``.then(m => m.X)`` class and the imported file,
    each when it can be read. A child is looked up by its file first, then by its classes."""
    text = node_text(load, source)
    found: list[Target] = []
    m = _MOUNT_IMPORT_RE.search(text)
    if m is not None and repo_root is not None and m.group(1).startswith("."):
        resolved = _try_paths((repo_root / rel).parent / m.group(1), repo_root)
        if resolved is not None:
            found.append(("file", resolved))
    m = _MOUNT_TARGET_RE.search(text)
    if m is not None:
        found.append(("symbol", m.group(1)))
    return found


def _collect_route_mounts(root: Node, source: bytes, rel: str,
                          repo_root: Path | None) -> tuple[list[str], list[_MountRaw]]:
    """(top-level class names declared here, lazy mounts declared here). A mount is a
    ``loadChildren`` pair inside a route object; its targets are the loaded file and class,
    and its own path is the sibling ``path:`` value (as unresolved pieces, None if dynamic)."""
    classes: list[str] = []
    mounts: list[_MountRaw] = []

    def walk(node: Node) -> None:
        if node.type in _CLASS_NODES:
            nm = node.child_by_field_name("name")
            if nm is not None:
                classes.append(node_text(nm, source))
        if node.type == "object":  # a route config object literal
            pairs = {}
            for c in node.named_children:
                if c.type == "pair":
                    k = c.child_by_field_name("key")
                    if k is not None:
                        pairs[node_text(k, source).strip("'\"")] = c.child_by_field_name("value")
            lc = pairs.get("loadChildren")
            if lc is not None and "path" in pairs:
                targets = _targets(lc, source, rel, repo_root)
                if targets:
                    mounts.append((targets, _path_pieces(pairs["path"], source)))
        for c in node.named_children:
            walk(c)

    walk(root)
    return classes, mounts


_CLASS_NODES_SET = frozenset(_CLASS_NODES)


def _ngmodule_import_names(dec: Node, source: bytes) -> list[str] | None:
    """Given a ``decorator`` node, return the list of identifier names from its
    ``@NgModule({imports: [...]})`` array, or None if it is not an NgModule decorator."""
    call = next((c for c in dec.named_children if c.type == "call_expression"), None)
    if call is None:
        return None
    fn = call.child_by_field_name("function")
    if fn is None or node_text(fn, source) != "NgModule":
        return None
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    config = next((c for c in args.named_children if c.type == "object"), None)
    if config is None:
        return None
    for pair in config.named_children:
        if pair.type != "pair":
            continue
        k = pair.child_by_field_name("key")
        v = pair.child_by_field_name("value")
        if k is None or node_text(k, source).strip("'\"") != "imports":
            continue
        if v is None or v.type != "array":
            continue
        return [node_text(c, source) for c in v.named_children if c.type == "identifier"]
    return None


def _collect_ngmodule_imports(root: Node, source: bytes) -> dict[str, list[str]]:
    """Return a map of class name → list of identifier names from the class's
    ``@NgModule({imports: [...]})`` decorator. Only top-level class declarations
    are considered (the Angular module pattern). Returns empty dict if no NgModule found."""
    if b"NgModule" not in source:
        return {}
    result: dict[str, list[str]] = {}
    pending: list[str] | None = None  # imports from the most recent @NgModule decorator

    for child in root.named_children:
        if child.type == "decorator":
            pending = _ngmodule_import_names(child, source)
            continue
        if child.type == "comment":
            continue  # comments don't clear pending decorators (mirrors TypeScriptParser.extract)

        # inline decorator inside export_statement (export @NgModule class Foo {})
        if child.type == "export_statement":
            inner_dec = next((c for c in child.named_children if c.type == "decorator"), None)
            if inner_dec is not None:
                imports = _ngmodule_import_names(inner_dec, source)
                if imports is not None:
                    pending = imports
            cls_node = next((c for c in child.named_children if c.type in _CLASS_NODES_SET), None)
        else:
            cls_node = child if child.type in _CLASS_NODES_SET else None

        if cls_node is not None and pending is not None:
            nm = cls_node.child_by_field_name("name")
            if nm is not None:
                result[node_text(nm, source)] = pending
        pending = None

    return result


def _compose_route_mounts(
    own: dict[Target, str | None], parent: dict[Target, tuple[Target, ...] | None]
) -> dict[Target, str | None]:
    """Chain-compose each target's own prefix with its ancestors' to a full path. ``own[t]`` is
    the target's own mount prefix (None if ambiguous / unresolved); ``parent[t]`` is the keys of
    the file that mounts it (its file and its classes; None if mounted from two files). The
    first of those keys that is itself mounted is the enclosing mount; none mounted → ``t`` is
    mounted from a routing root and its own prefix is the path. Anything reached through an
    unresolved link is None. A mount cycle (shouldn't happen in routing) resolves to None."""
    out: dict[Target, str | None] = {}

    def resolve(target: Target, seen: frozenset[Target]) -> str | None:
        if target in out:
            return out[target]
        prefix = own.get(target)
        keys = parent.get(target)
        if prefix is None or keys is None or target in seen:
            return None
        outer = next((k for k in keys if k in own), None)
        if outer is not None:
            above = resolve(outer, seen | {target})
            prefix = None if above is None else _join_segments(above, prefix)
        out[target] = prefix
        return prefix

    for target in sorted(own):
        out[target] = resolve(target, frozenset())
    return out


def route_mounts(index: Any | None) -> dict[Target, str | None]:
    return index_fact(index, FACT) or {}


#: One file's mount facts: (its path, top-level classes, lazy mounts, @NgModule class → imports).
_FileMounts = tuple[str, list[str], list[_MountRaw], dict[str, list[str]]]


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileMounts | None:
    classes, mounts = _collect_route_mounts(root, source, rel, repo_root)
    ngmodule_imports = _collect_ngmodule_imports(root, source)
    if not mounts and not ngmodule_imports:
        return None
    return rel, classes, mounts, ngmodule_imports


def _resolve(values: list[_FileMounts], index: Any) -> dict[Target, str | None] | None:
    const_values: dict[str, str | None] = getattr(index, "const_values", None) or {}
    # ``own``: target → its own mount prefix; ``parent``: target → the keys of the file that
    # mounts it. A target mounted at >1 distinct prefix or from >1 file, or with an unresolved
    # path, collapses to None (record_distinct): its routes get no address, never a wrong one.
    own: dict[Target, str | None] = {}
    parent: dict[Target, tuple[Target, ...] | None] = {}
    all_ngmodule_imports: dict[str, list[str]] = {}
    for rel, classes, mounts, ngmodule_imports in values:
        here: tuple[Target, ...] = (("file", rel), *(("symbol", c) for c in classes))
        for targets, pieces in mounts:
            prefix = _resolve_pieces(pieces, const_values) if pieces is not None else None
            for target in targets:
                record_distinct(own, target, prefix)
                record_distinct(parent, target, here)
        all_ngmodule_imports.update(ngmodule_imports)
    # NgModule propagation: when BrandModule is a loadChildren target and its @NgModule imports
    # BrandRoutingModule, BrandRoutingModule is mounted at the same place (same own prefix and
    # parent). Repeated to a fixpoint so a routing module imported through another module is
    # reached too; ProductsModule mounted by BrandRoutingModule then chains up to
    # /company/:brandId/products instead of stopping at /products.
    changed = True
    while changed:
        changed = False
        for class_name, ngmod_imports in all_ngmodule_imports.items():
            target = ("symbol", class_name)
            if target not in own:
                continue
            for imported in ngmod_imports:
                key = ("symbol", imported)
                if key not in own:
                    own[key] = own[target]
                    parent[key] = parent.get(target)
                    changed = True

    return _compose_route_mounts(own, parent) or None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b"loadChildren", b"NgModule"),
    collect=_collect, resolve=_resolve,
))
