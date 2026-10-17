"""GraphQL resolver maps assembled from modules, as a registered fact on the TypeScript index,
``graphql.resolver-modules``, and the detector that reads it.

A resolver map's root type can be a whole module rather than an object literal: every name the
module exports is one operation, often re-exported through a barrel::

    // apolloServer.ts
    import * as queries from './resolvers/queries';
    const resolvers = { Query: queries };                  // → QUERY dummyQuery → dummyQuery
    // resolvers/queries/index.ts
    export { default as dummyQuery } from './dummyQuery';
    // resolvers/queries/dummyQuery.ts
    async function dummyQuery(_, args) { … }
    export default dummyQuery;

While the TypeScript index is built, ``_collect`` reads each file's exports (declared here,
re-exported from another file, or ``export *``) and each root key whose value is a namespace
import. ``_resolve`` follows the re-export chains for the modules used that way only, and maps
``(file, local name)`` to the module's operations: (exported name, handler). A handler is the
function's own name, or None when the exported function is anonymous. A module that can't be
found gives no operations (honest-null), never a guessed list.

The detector runs on every TypeScript file, since such a file has neither a ``Resolvers`` type
nor SDL and so is not claimed by the GraphQL parser. It records one operation per exported name
on the root key's ``pair``; object-literal roots stay with :func:`.routes.detect_resolver_maps`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...emit import file_id
from ..additive import (
    DetectContext, Detection, Detector, FactSpec, Gate, index_fact, register_detector,
    register_fact,
)
from ..emits import Emit
from ..treesitter import first_line, node_text
from ..typescript.imports import _is_fixture_path, _module_of, _try_paths
from .sdl_fields import root_field_dto

FACT = "graphql.resolver-modules"

# The root operation types (mirrors ``routes._ROOT_TYPES``).
_ROOT_TYPES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}

#: One export of a file: ``("local", handler or None)`` — declared here — or
#: ``("from", file, name)`` — re-exported from ``file`` under its ``name`` there.
Export = tuple[str, ...]
#: One file's facts: (path, exported name → Export, ``export *`` files, namespace roots
#: as (local name, imported file)).
_FileExports = tuple[str, dict[str, Export], list[str], list[tuple[str, str]]]


def _resolve_module(spec: str | None, rel: str, repo_root: Path | None) -> str | None:
    if spec is None or repo_root is None or not spec.startswith("."):
        return None
    return _try_paths((repo_root / rel).parent / spec, repo_root)


def _exports(root: Node, source: bytes, rel: str,
             repo_root: Path | None) -> tuple[dict[str, Export], list[str]]:
    exports: dict[str, Export] = {}
    stars: list[str] = []
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        src = node.child_by_field_name("source")
        target = _resolve_module(node_text(src, source).strip("'\"`"), rel, repo_root) \
            if src is not None else None
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        if clause is not None:
            for spec in clause.named_children:
                if spec.type != "export_specifier":
                    continue
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias") or name
                if name is None or alias is None:
                    continue
                if src is None:
                    exports[node_text(alias, source)] = ("local", node_text(name, source))
                elif target is not None:
                    exports[node_text(alias, source)] = ("from", target, node_text(name, source))
            continue
        if src is not None:  # `export * from './x'` (`* as ns` is one name, not a spread)
            if target is not None and not any(c.type == "namespace_export"
                                              for c in node.named_children):
                stars.append(target)
            continue
        decl = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")
        if any(c.type == "default" for c in node.children):
            # `export default dummyQuery;` · `export default function f() {}` · an anonymous
            # function (no handler name).
            inner = decl or value
            nm = inner.child_by_field_name("name") if inner is not None and \
                inner.type != "identifier" else inner
            exports["default"] = ("local", node_text(nm, source) if nm is not None else "")
        elif decl is not None:
            for declared in _declared_names(decl, source):
                exports[declared] = ("local", declared)
    return exports, stars


def _declared_names(decl: Node, source: bytes) -> list[str]:
    if decl.type in ("function_declaration", "generator_function_declaration",
                     "class_declaration"):
        nm = decl.child_by_field_name("name")
        return [node_text(nm, source)] if nm is not None else []
    if decl.type in ("lexical_declaration", "variable_declaration"):
        return [node_text(nm, source) for d in decl.named_children
                if d.type == "variable_declarator"
                and (nm := d.child_by_field_name("name")) is not None and nm.type == "identifier"]
    return []


def _namespace_imports(root: Node, source: bytes, rel: str,
                       repo_root: Path | None) -> dict[str, str]:
    """``import * as queries from './q'`` → ``{"queries": "<resolved q file>"}``."""
    out: dict[str, str] = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        ns = next((c for c in clause.named_children if c.type == "namespace_import"), None) \
            if clause is not None else None
        local = next((c for c in ns.named_children if c.type == "identifier"), None) \
            if ns is not None else None
        target = _resolve_module(_module_of(node, source), rel, repo_root)
        if local is not None and target is not None:
            out[node_text(local, source)] = target
    return out


def _module_roots(root: Node, source: bytes) -> list[tuple[Node, str, str]]:
    """Each ``Query: queries`` pair whose value is an identifier: (pair, root type, local)."""
    found: list[tuple[Node, str, str]] = []

    def walk(node: Node) -> None:
        if node.type == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            name = node_text(key, source).strip("'\"`") if key is not None else None
            if name in _ROOT_TYPES and value is not None and value.type == "identifier":
                found.append((node, name, node_text(value, source)))
        for c in node.named_children:
            walk(c)

    walk(root)
    return found


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileExports | None:
    if _is_fixture_path(rel):
        return None
    exports, stars = _exports(root, source, rel, repo_root)
    roots: list[tuple[str, str]] = []
    if any(f"{t}:".encode() in source for t in _ROOT_TYPES):
        imports = _namespace_imports(root, source, rel, repo_root)
        roots = [(local, imports[local]) for _, _, local in _module_roots(root, source)
                 if local in imports]
    if not exports and not stars and not roots:
        return None
    return rel, exports, stars, roots


#: The fact: (file, local name) → the module's operations, (exported name, handler or None).
Operations = tuple[tuple[str, str | None], ...]


def _resolve(values: list[_FileExports], index: Any) -> dict[tuple[str, str], Operations] | None:
    exports = {rel: ex for rel, ex, _, _ in values}
    stars = {rel: st for rel, _, st, _ in values}

    def handler(file: str, name: str, seen: frozenset[tuple[str, str]]) -> str | None:
        """The handler ``name`` in ``file`` resolves to: a function name, ``""`` for an anonymous
        function, None when the chain can't be followed."""
        if (file, name) in seen:
            return None
        entry = exports.get(file, {}).get(name)
        if entry is None:
            if name == "default":
                return None
            for star in stars.get(file, []):  # first `export *` that provides it
                found = handler(star, name, seen | {(file, name)})
                if found is not None:
                    return found
            return None
        if entry[0] == "local":
            return entry[1]
        return handler(entry[1], entry[2], seen | {(file, name)})

    def names(file: str, seen: frozenset[str]) -> list[str]:
        if file in seen:
            return []
        own = [n for n in exports.get(file, {}) if n != "default"]
        for star in stars.get(file, []):
            own += [n for n in names(star, seen | {file}) if n not in own]
        return own

    out: dict[tuple[str, str], Operations] = {}
    for rel, _, _, roots in values:
        for local, module in roots:
            if module not in exports and module not in stars:
                continue  # not indexed (honest-null)
            ops = []
            for name in names(module, frozenset()):
                found = handler(module, name, frozenset())
                if found is not None:
                    ops.append((name, found or None))
            if ops:
                out[(rel, local)] = tuple(ops)
    return out or None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b"export", b"Query:", b"Mutation:", b"Subscription:"),
    collect=_collect, resolve=_resolve,
))


def _detect(dc: DetectContext) -> Detection | None:
    fact: dict[tuple[str, str], Operations] = index_fact(dc.ctx.resolution_index, FACT) or {}
    if not fact or not any(f"{t}:".encode() in dc.source for t in _ROOT_TYPES):
        return None
    routes: list[Emit] = []
    for pair, root_type, local in _module_roots(dc.root, dc.source):
        kind = _ROOT_TYPES[root_type]
        for op, handler in fact.get((dc.path, local), ()):
            request_dto, response_dto = root_field_dto(dc.ctx.resolution_index, root_type, op)
            # handlerLine stays empty: the function is declared in another file.
            routes.append(Emit(pair, dict(
                semanticType="route", method=kind.upper(), endpoint=op,
                requestDTO=request_dto, responseDTO=response_dto, framework="graphql",
                handler=handler, routeKind=kind,
            ), parent=file_id(dc.path), text=first_line(node_text(pair, dc.source))[:120]))
    return Detection(routes, label="graphql") if routes else None


register_detector(Detector(
    name="graphql-resolver-modules",
    gate=Gate("typescript", order=45, guard=None, skip_fixtures=True),
    detect=_detect, frameworks=("graphql",),
))
