"""GraphQL resolver maps assembled from modules: a root type given as a namespace import or by name.

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

It can also be an object declared in another file and imported by name; each property is one
operation::

    // apolloServer.ts
    import { queries } from './queries';
    const resolvers = { Query: queries };                  // → QUERY books
    // queries.ts
    export const queries = { books: async () => [] };      // line 1

The ``typescript.declarations`` fact finds the object's declaration and ``graphql.resolver-objects``
(:mod:`.resolver_objects`) its properties, so each record leads to where its function starts in that
file. ``handler`` is set for a shorthand method only: an inline function is anonymous, and a
property that is not an inline function (``books: getBooks``) is not named, for the name would be
looked up in the wrong file. A name that is not a resolver object gives no operations. A default
import, and an object declared in the same file, are not read.

A root key can be written shorthand (``{ Query, Mutation }`` after ``import { Query } from
'./Query'``): the key is then the local name. A *named* object is read only where it is GraphQL
and not another ``Query`` map (a CQRS bus, Apollo client ``typePolicies``): the object holding the
key is named like a resolver map (``const resolvers = {…}``, ``resolvers: {…}``, ``: Resolvers``),
or the repo declares GraphQL SDL root fields. A namespace import is read as before.

The detector runs on every TypeScript file, since such a file has neither a ``Resolvers`` type
nor SDL and so is not claimed by the GraphQL parser. It records one operation per exported name
or property on the root key's ``pair``; object-literal roots stay with
:func:`.routes.detect_resolver_maps`.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import file_id
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import first_line, node_text
from ..handler_sites import handler_location, locate_export
from ..typescript.declarations import export_names, import_bindings
from ..typescript.imports import TsAliasIndex, _resolve
from .resolver_objects import resolver_object
from .sdl_fields import declares_root_fields, root_field_dto

# The root operation types (mirrors ``routes._ROOT_TYPES``).
_ROOT_TYPES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}

# A root key as the file writes it: ``Query:`` (``Query: queries``) or ``Query`` closing a
# shorthand entry (``{ Query, Mutation }``). A cheap filter before the walk; ``useQuery`` and
# ``QueryClient`` don't match.
_ROOT_KEY = re.compile(rb"\b(?:Query|Mutation|Subscription)\b\s*[:,}]")
_WRAPPERS = ("as_expression", "satisfies_expression", "parenthesized_expression")

#: One operation of an imported root: (endpoint, handler, the (file, line) its function starts at).
_Operation = tuple[str, str | None, tuple[str, int] | None]


def _module_roots(root: Node, source: bytes) -> list[tuple[Node, str, str]]:
    """Each ``Query: queries`` pair whose value is an identifier, and each shorthand ``{ Query }``
    (the key is the local name): (node, root type, local)."""
    found: list[tuple[Node, str, str]] = []

    def walk(node: Node) -> None:
        if node.type == "shorthand_property_identifier":
            shorthand = node_text(node, source)
            if shorthand in _ROOT_TYPES:
                found.append((node, shorthand, shorthand))
        elif node.type == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            name = node_text(key, source).strip("'\"`") if key is not None else None
            if name in _ROOT_TYPES and value is not None and value.type == "identifier":
                found.append((node, name, node_text(value, source)))
        for c in node.named_children:
            walk(c)

    walk(root)
    return found


def _namespace_operations(index: TsAliasIndex | None, module: str) -> list[_Operation]:
    """Every name the module exports, each led to its declaration."""
    operations: list[_Operation] = []
    for name in export_names(index, module):
        decl = locate_export(index, module, name)
        if decl is not None:
            operations.append((name, decl.name, (decl.file, decl.line)))
    return operations


def _named_operations(index: TsAliasIndex | None, module: str, export: str) -> list[_Operation]:
    """The properties of the resolver object that ``module`` exports as ``export``."""
    decl = locate_export(index, module, export)
    if decl is None or decl.name is None:
        return []
    return [(name, handler, (decl.file, line) if line is not None else None)
            for name, handler, line in resolver_object(index, decl.file, decl.name)]


def _in_resolvers_map(root_key: Node, source: bytes) -> bool:
    """Whether the object holding this root key is named like a resolver map: ``const resolvers
    = {…}``, ``resolvers: {…}``, or one typed ``Resolvers`` (``: Resolvers =``, ``as Resolvers``)
    """
    obj = root_key.parent
    holder = obj.parent if obj is not None else None
    while holder is not None and holder.type in _WRAPPERS:
        holder = holder.parent
    if obj is None or holder is None or holder.type not in ("variable_declarator", "pair"):
        return False
    around = source[holder.start_byte : obj.start_byte] + source[obj.end_byte : holder.end_byte]
    return b"resolvers" in around.lower()


def _route(dc: DetectContext, pair: Node, root_type: str, operation: _Operation) -> Emit:
    endpoint, handler, site = operation
    kind = _ROOT_TYPES[root_type]
    handler_line, handler_path = handler_location(site, dc.path)
    request_dto, response_dto = root_field_dto(dc.ctx.resolution_index, root_type, endpoint)
    return Emit(pair, dict(
        semanticType="route", method=kind.upper(), endpoint=endpoint,
        requestDTO=request_dto, responseDTO=response_dto, framework="graphql",
        handler=handler, handlerLine=handler_line, handlerPath=handler_path,
        routeKind=kind,
    ), parent=file_id(dc.path), text=first_line(node_text(pair, dc.source))[:120])


def _detect(dc: DetectContext) -> Detection | None:
    if b"import" not in dc.source or _ROOT_KEY.search(dc.source) is None:
        return None
    index = dc.ctx.resolution_index
    bindings = import_bindings(dc.root, dc.source)
    routes: list[Emit] = []
    for pair, root_type, local in _module_roots(dc.root, dc.source):
        spec, export = bindings.get(local, (None, None))
        module = _resolve(spec, dc.path, dc.ctx.repo_root, index) if spec is not None else None
        if module is None or export is None or export == "default":
            continue
        # Under a `Query` key a named object is GraphQL only with a resolver map or a schema.
        if export != "*" and not (
            _in_resolvers_map(pair, dc.source) or declares_root_fields(index)
        ):
            continue
        operations = (_namespace_operations(index, module) if export == "*"
                      else _named_operations(index, module, export))
        routes += [_route(dc, pair, root_type, op) for op in operations]
    return Detection(routes, label="graphql") if routes else None


register_detector(Detector(
    name="graphql-resolver-modules",
    gate=Gate("typescript", order=45, guard=None, skip_fixtures=True),
    detect=_detect, frameworks=("graphql",),
))
