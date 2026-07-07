"""GraphQL route detection (Apollo / graphql-tools resolver maps + SDL).

GraphQL is the primary API surface of Apollo/`graphql-tools` backends, but its
operations are neither HTTP-verb calls (Express) nor decorated methods (NestJS) —
they live in two shapes, in separate files:

* **Resolver map** (the implementation, carries the handler)::

      export const resolvers: Resolvers = {
        Query:    { procurementItem: async (_, { id }, ctx) => … },
        Mutation: { createProcurementItem: (_, { input }, ctx) => … },
      };

  Each field of the ``Query`` / ``Mutation`` / ``Subscription`` object is one
  operation. The value is (usually) an anonymous arrow function that the base
  parser skips as a nested scope, so we surface it here — ``handler`` is the
  operation name, ``handlerLine`` the arrow's line. Type-level field resolvers
  (``ProcurementItem: { tenders: … }``) are **not** routes and are ignored.

* **SDL** (the interface, carries request/response DTOs)::

      const typeDefs = gql`
        type Query    { procurementItems(filter: ProcurementFilter): [ProcurementItem!]! }
        type Mutation { createProcurementItem(input: CreateProcurementItemInput!): ProcurementItem! }
      `;

  The SDL lives in an opaque ``template_string``; we re-parse it with the dedicated
  ``graphql`` grammar and walk the SDL AST, so each field yields a route with
  ``requestDTO`` (an ``input:``/first arg type) and ``responseDTO`` (the return type),
  wrappers stripped. ``extend type Query`` is also matched.

Both emit ``semanticType="route"``, ``framework="graphql"``,
``routeKind ∈ {query, mutation, subscription}``, ``method`` the upper-cased
operation kind (GraphQL has no per-operation HTTP verb — the transport is a
single ``POST /graphql``, detected separately by the Express mount). Resolver
and SDL routes for the same operation live in different files; the backend joins
them on ``(framework, routeKind, endpoint)`` — the handler from the resolver, the
DTOs from the SDL. Routes are parented to the file (config/markup, not handler
methods), mirroring the React/Angular config detectors."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import first_line, node_text, parse_source

# The three root operation types. Everything else keyed in a resolver map
# (``ProcurementItem``, ``Tender``, scalars, …) is a field/scalar resolver, not a route.
_ROOT_TYPES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}


# ---- resolver-map form ------------------------------------------------------

def _key_text(node: Node, source: bytes) -> str | None:
    """Property key of a ``pair`` / ``method_definition`` -> its name (quotes stripped)."""
    key = node.child_by_field_name("key") or node.child_by_field_name("name")
    if key is None:
        return None
    text = node_text(key, source)
    if key.type == "string":
        return text.strip("'\"`")
    return text


def _operation_fields(group_obj: Node) -> list[Node]:
    """The operation entries of a ``Query``/``Mutation``/``Subscription`` object —
    both ``pair`` (arrow/function value) and ``method_definition`` (shorthand) forms."""
    return [c for c in group_obj.named_children if c.type in ("pair", "method_definition")]


def _handler_line(field: Node) -> int:
    """Line of the field's implementation — the value node for a pair, else the field."""
    value = field.child_by_field_name("value")
    node = value if value is not None else field
    return node.start_point[0] + 1


def _detect_resolver_maps(root: Node, source: bytes, path: str, seen: set[str]) -> list[Statement]:
    routes: list[Statement] = []

    def walk(node: Node) -> None:
        if node.type == "pair":
            key = _key_text(node, source)
            value = node.child_by_field_name("value")
            if key in _ROOT_TYPES and value is not None and value.type == "object":
                kind = _ROOT_TYPES[key]
                for field in _operation_fields(value):
                    op = _key_text(field, source)
                    if op is None:
                        continue
                    sl, sc = field.start_point[0] + 1, field.start_point[1]
                    routes.append(Statement(
                        id=disambiguate(statement_id(path, sl, sc), seen),
                        parentId=file_id(path),
                        nodeType=field.type,
                        semanticType="route",
                        text=first_line(node_text(field, source))[:120],
                        method=kind.upper(),
                        endpoint=op,
                        framework="graphql",
                        handler=op,
                        handlerLine=_handler_line(field),
                        routeKind=kind,
                        startLine=sl,
                        endLine=field.end_point[0] + 1,
                        path=path,
                    ))
        for c in node.named_children:
            walk(c)

    walk(root)
    return routes


# ---- SDL form ---------------------------------------------------------------
#
# SDL lives inside a ``gql``/``graphql`` template literal, which the TypeScript
# grammar treats as an opaque ``template_string`` — so we locate that string node in
# the TS tree, then re-parse its text with the dedicated ``graphql`` grammar and walk
# the SDL AST (``object_type_definition`` / ``object_type_extension`` -> ``name`` +
# ``fields_definition`` -> ``field_definition``). GraphQL field children are unnamed,
# so navigation is by node ``type``.

# A template_string is worth re-parsing as SDL only if it declares a root type.
_SDL_MARKERS = (b"type Query", b"type Mutation", b"type Subscription")


def _child(node: Node, typ: str) -> Node | None:
    return next((c for c in node.named_children if c.type == typ), None)


def _base_type_name(node: Node | None, source: bytes) -> str | None:
    """The underlying type name of a ``type`` node, stripping ``!``/``[]`` wrappers:
    ``[ProcurementItem!]!`` -> ``ProcurementItem``. Recurses to the first ``named_type``."""
    if node is None:
        return None
    if node.type == "named_type":
        name = _child(node, "name")
        return node_text(name, source) if name is not None else None
    for c in node.named_children:
        found = _base_type_name(c, source)
        if found is not None:
            return found
    return None


def _request_dto(field: Node, source: bytes) -> str | None:
    """The input DTO of a field's args — the ``input``/``data`` arg if present, else the
    first arg — as its base type name."""
    args = _child(field, "arguments_definition")
    if args is None:
        return None
    inputs = [c for c in args.named_children if c.type == "input_value_definition"]
    if not inputs:
        return None
    chosen = next(
        (i for i in inputs
         if (n := _child(i, "name")) is not None and node_text(n, source) in ("input", "data")),
        inputs[0],
    )
    return _base_type_name(_child(chosen, "type"), source)


def _emit_fields(obj: Node, kind: str, sdl: bytes, row_base: int, path: str,
                 seen: set[str], routes: list[Statement]) -> None:
    fields_def = _child(obj, "fields_definition")
    if fields_def is None:
        return
    for field in fields_def.named_children:
        if field.type != "field_definition":
            continue
        name = _child(field, "name")
        if name is None:
            continue
        # Anchor the line to the field name, not the field_definition span (which the
        # grammar extends up over any leading """description""" block). SDL rows are
        # 0-based within the fragment; row_base is the fragment's TS row.
        line = row_base + name.start_point[0] + 1
        routes.append(Statement(
            id=disambiguate(statement_id(path, line, name.start_point[1]), seen),
            parentId=file_id(path),
            nodeType="graphql_field",
            semanticType="route",
            text=first_line(node_text(field, sdl))[:120],
            method=kind.upper(),
            endpoint=node_text(name, source=sdl),
            framework="graphql",
            routeKind=kind,
            requestDTO=_request_dto(field, sdl),
            responseDTO=_base_type_name(_child(field, "type"), sdl),
            startLine=line,
            endLine=row_base + field.end_point[0] + 1,
            path=path,
        ))


def _parse_sdl_fragment(frag: Node, source: bytes, path: str, seen: set[str],
                        routes: list[Statement], timeout_micros: int) -> None:
    """Re-parse one ``string_fragment``'s bytes with the ``graphql`` grammar and emit a
    route per root-type field. Line numbers map back via the fragment's TS start row."""
    sdl = source[frag.start_byte:frag.end_byte]
    row_base = frag.start_point[0]
    gql_root = parse_source("graphql", sdl, timeout_micros).root_node

    def walk(n: Node) -> None:
        if n.type in ("object_type_definition", "object_type_extension"):
            name = _child(n, "name")
            kind = _ROOT_TYPES.get(node_text(name, sdl)) if name is not None else None
            if kind is not None:
                _emit_fields(n, kind, sdl, row_base, path, seen, routes)
        for c in n.named_children:
            walk(c)

    walk(gql_root)


def _detect_sdl(root: Node, source: bytes, path: str, seen: set[str],
                timeout_micros: int) -> list[Statement]:
    routes: list[Statement] = []

    def walk(n: Node) -> None:
        if n.type == "string_fragment":
            frag = source[n.start_byte:n.end_byte]
            if any(m in frag for m in _SDL_MARKERS):
                _parse_sdl_fragment(n, source, path, seen, routes, timeout_micros)
        for c in n.named_children:
            walk(c)

    walk(root)
    return routes


# ---- entry point ------------------------------------------------------------

def detect_graphql(root: Node, source: bytes, path: str, *, seen_ids: set[str],
                   timeout_micros: int = 0) -> list[Statement]:
    """All GraphQL routes in the file — resolver-map operations (with handlers) plus
    SDL operations (with DTOs). ``timeout_micros`` bounds the secondary ``graphql`` parse
    of embedded SDL, threaded from ``ctx.parse_timeout_micros`` like every other
    ``parse_source`` call. Returns statements to append to the record."""
    routes = _detect_resolver_maps(root, source, path, seen_ids)
    routes += _detect_sdl(root, source, path, seen_ids, timeout_micros)
    return routes
