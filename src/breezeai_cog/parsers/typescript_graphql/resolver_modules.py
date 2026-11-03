"""GraphQL resolver maps assembled from modules: a root type given as a namespace import.

A resolver map's root type can be a whole module rather than an object literal: every name the
module exports is one operation, often re-exported through a barrel::

    // apolloServer.ts
    import * as queries from './resolvers/queries';
    const resolvers = { Query: queries };                  // → QUERY dummyQuery
    // resolvers/queries/index.ts
    export { default as dummyQuery } from './dummyQuery';
    // resolvers/queries/dummyQuery.ts
    async function dummyQuery(_, args) { … }               // line 3
    export default dummyQuery;

The module's exports and where each is declared come from the ``typescript.declarations`` fact
(re-exports followed), so each operation's record leads to its function: ``handler``,
``handlerPath``, ``handlerLine``. A module outside the repo gives no operations (honest-null).

The detector runs on every TypeScript file, since such a file has neither a ``Resolvers`` type
nor SDL and so is not claimed by the GraphQL parser. It records one operation per exported name
on the root key's ``pair``; object-literal roots stay with :func:`.routes.detect_resolver_maps`.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import first_line, node_text
from ..handler_sites import handler_location, locate_export
from ..typescript.declarations import export_names, import_bindings
from ..typescript.imports import _resolve
from .sdl_fields import root_field_dto

# The root operation types (mirrors ``routes._ROOT_TYPES``).
_ROOT_TYPES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}


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


def _detect(dc: DetectContext) -> Detection | None:
    if b"import *" not in dc.source or not any(f"{t}:".encode() in dc.source
                                                for t in _ROOT_TYPES):
        return None
    index = dc.ctx.resolution_index
    namespaces = {local: spec for local, (spec, exp) in import_bindings(dc.root, dc.source).items()
                  if exp == "*"}
    routes: list[Emit] = []
    for pair, root_type, local in _module_roots(dc.root, dc.source):
        spec = namespaces.get(local)
        module = _resolve(spec, dc.path, dc.ctx.repo_root, index) if spec is not None else None
        if module is None:
            continue
        kind = _ROOT_TYPES[root_type]
        for op in export_names(index, module):
            decl = locate_export(index, module, op)
            if decl is None:
                continue
            handler_line, handler_path = handler_location((decl.file, decl.line), dc.path)
            request_dto, response_dto = root_field_dto(index, root_type, op)
            routes.append(Emit(pair, dict(
                semanticType="route", method=kind.upper(), endpoint=op,
                requestDTO=request_dto, responseDTO=response_dto, framework="graphql",
                handler=decl.name, handlerLine=handler_line, handlerPath=handler_path,
                routeKind=kind,
            ), parent=file_id(dc.path), text=first_line(node_text(pair, dc.source))[:120]))
    return Detection(routes, label="graphql") if routes else None


register_detector(Detector(
    name="graphql-resolver-modules",
    gate=Gate("typescript", order=45, guard=None, skip_fixtures=True),
    detect=_detect, frameworks=("graphql",),
))
