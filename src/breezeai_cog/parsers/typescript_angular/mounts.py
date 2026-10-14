"""Angular lazy-route mounts as a registered fact on the TypeScript index,
``angular.route-mounts``.

A lazy child module's routes are served under the path its parent mounts it at, in another
file::

    // app-routing.module.ts
    { path: 'company/:brandId', loadChildren: () => import('./brand/brand.module')
        .then(m => m.BrandModule) }
    // brand.module.ts:  @NgModule({ imports: [BrandRoutingModule] }) export class BrandModule {}

The fact maps a routing-module class name to its **fully composed** mount prefix (the parent
chain prepended, ``NgModule`` imports propagated), so a child module parsed in its own file
starts at that prefix instead of ``""``. A symbol mounted at more than one prefix, or with an
unresolved chain, is absent (honest-null — the child keeps its own bare paths). Paths may name
constants of other files (``RouteNames.X``), so they resolve against the index's constant map.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import _CLASS_NODES, _join_segments, _path_pieces, _resolve_pieces

FACT = "angular.route-mounts"

# A path expression is returned as UNRESOLVED pieces — ``("lit", text)`` for a string literal
# and ``("sym", "RouteNames.X")`` for a const reference — because ``RouteNames.*`` is defined
# cross-file, so the pieces are resolved against the repo-wide ``const_values`` in the reduce
# (:func:`~..typescript.imports._resolve_pieces`), not here.
_MountRaw = tuple[str, list[tuple[str, str]]]  # (target module symbol, path pieces)

# `.then(m => m.OrganisationsRoutingModule)` — the mounted module's exported class symbol.
_MOUNT_TARGET_RE = re.compile(r"=>\s*[\w$]+\.([A-Za-z_$][\w$]*)")


def _collect_route_mounts(root: Node, source: bytes) -> tuple[list[str], list[_MountRaw]]:
    """(top-level class names declared here, lazy mounts declared here). A mount is a
    ``loadChildren`` pair inside a route object; its target is the ``.then(m => m.X)`` symbol
    and its own path is the sibling ``path:`` value (as unresolved pieces)."""
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
            if "loadChildren" in pairs and "path" in pairs:
                lc = pairs["loadChildren"]
                m = _MOUNT_TARGET_RE.search(node_text(lc, source)) if lc is not None else None
                pieces = _path_pieces(pairs["path"], source)
                if m is not None and pieces is not None:
                    mounts.append((m.group(1), pieces))
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
    raw: dict[str, str | None], parent_of: dict[str, str | None]
) -> dict[str, str]:
    """Chain-compose each mount's own prefix with its ancestors' prefixes to a full effective
    path. ``raw[sym]`` is the module's OWN mount prefix (None if ambiguous/unresolved);
    ``parent_of[sym]`` is the module symbol whose mount encloses it (None if none/ambiguous).
    A module reached through an unresolved link → dropped (honest-null). Iterates to a fixpoint,
    bounded by chain length; a cycle (shouldn't happen in routing) just stops resolving."""
    composed: dict[str, str] = {}
    for _ in range(len(raw) + 1):  # bounded: converges in <= chain-depth passes
        progressed = False
        for sym, own in raw.items():
            if sym in composed or own is None:
                continue
            parent = parent_of.get(sym)
            # A root boundary: no parent, an ambiguous parent (None), or a parent that is not
            # itself a mounted module (e.g. the top AppRoutingModule) → own prefix IS the path.
            if parent is None or parent not in raw:
                composed[sym] = own
                progressed = True
            elif parent in composed:  # ancestor resolved → prepend its full chain
                composed[sym] = _join_segments(composed[parent], own)
                progressed = True
        if not progressed:
            break
    return composed


def route_mounts(index: Any | None) -> dict[str, str]:
    return index_fact(index, FACT) or {}


#: One file's mount facts: (top-level classes, lazy mounts, @NgModule class → imports).
_FileMounts = tuple[list[str], list[_MountRaw], dict[str, list[str]]]


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileMounts | None:
    classes, mounts = _collect_route_mounts(root, source)
    ngmodule_imports = _collect_ngmodule_imports(root, source)
    if not mounts and not ngmodule_imports:
        return None
    return classes, mounts, ngmodule_imports


def _resolve(values: list[_FileMounts], index: Any) -> dict[str, str] | None:
    const_values: dict[str, str | None] = getattr(index, "const_values", None) or {}
    # ``own_prefix``: module symbol → its own mount prefix (ambiguity via record_distinct).
    # ``parent_of``: module symbol → the module whose file declared its mount.
    own_prefix: dict[str, str | None] = {}
    parent_of: dict[str, str | None] = {}
    all_ngmodule_imports: dict[str, list[str]] = {}
    for classes, mounts, ngmodule_imports in values:
        # Resolve each mount's path and wire the graph. A module symbol mounted at >1 distinct
        # prefix (or an unresolved path) collapses to None — the child then keeps its own bare
        # path (honest-null), never a wrongly-attributed prefix.
        enclosing = next(iter(frozenset(classes)), None)  # the routing module declared here
        for target, pieces in mounts:
            record_distinct(own_prefix, target, _resolve_pieces(pieces, const_values))
            if enclosing is not None:
                record_distinct(parent_of, target, enclosing)
        all_ngmodule_imports.update(ngmodule_imports)
    # Pre-composition NgModule propagation: if BrandModule is a loadChildren target and its
    # @NgModule imports BrandRoutingModule, inject BrandRoutingModule into own_prefix/parent_of
    # with the same own-path and parent as BrandModule BEFORE _compose_route_mounts runs.
    # This makes BrandRoutingModule a proper node in the composition graph, so that
    # ProductsModule (whose parent_of entry is BrandRoutingModule) chains correctly all the
    # way up to /company/:brandId/products instead of stopping at /products.
    for class_name, ngmod_imports in all_ngmodule_imports.items():
        own = own_prefix.get(class_name)
        if own is None:
            continue  # class not a loadChildren target, or path is ambiguous
        parent = parent_of.get(class_name)
        for imported in ngmod_imports:
            if imported not in own_prefix:
                record_distinct(own_prefix, imported, own)
                if parent is not None:
                    record_distinct(parent_of, imported, parent)

    mounts_out = _compose_route_mounts(own_prefix, parent_of)

    # Post-composition NgModule propagation: for modules that are now composed (e.g.
    # ProductsModule → /company/:brandId/products), also add their NgModule-imported routing
    # modules (e.g. ProductsRoutingModule) so the route detector finds them.
    for class_name, prefix in list(mounts_out.items()):
        for imported_name in all_ngmodule_imports.get(class_name, []):
            if imported_name not in mounts_out:
                mounts_out[imported_name] = prefix
    return mounts_out or None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b"loadChildren", b"NgModule"),
    collect=_collect, resolve=_resolve,
))
