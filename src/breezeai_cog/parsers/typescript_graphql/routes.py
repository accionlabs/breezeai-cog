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

import re

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text, parse_source

# The three root operation types. Everything else keyed in a resolver map
# (``ProcurementItem``, ``Tender``, scalars, …) is a field/scalar resolver, not a route.
_ROOT_TYPES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}

# Client-side operation keywords (lowercase) -> kind. Mirrors _ROOT_TYPES for the caller
# side (``query Foo {…}`` in a ``gql`` template) rather than the schema side.
_OP_KINDS = {"query": "query", "mutation": "mutation", "subscription": "subscription"}


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
            # SDL re-parsed from a gql`` template string — no node in the host (TS) AST, so
            # synthetic (not the GraphQL grammar's own node type).
            nodeType="synthetic",
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


# ---- client-operation form --------------------------------------------------
#
# A CLIENT operation is the caller side: a ``gql``/``graphql`` tagged template holding a
# ``query``/``mutation``/``subscription`` *operation* (not a ``type Query`` schema), e.g.
#
#     const GetSpec = gql`query specification($id: String!) { specification(id: $id) { ... } }`;
#
# invoked via ``apollo.query({ query: GetSpec })``. We re-parse the tagged template with the
# ``graphql`` grammar and walk ``operation_definition`` nodes. Unlike SDL, the invoked API is
# the operation's ROOT SELECTION FIELD (``specification``) — that is what matches a server
# route's ``endpoint``, so we emit one statement per root field and key ``endpoint`` on it
# (the operation NAME, e.g. ``GetSpecification``, is a client-side label kept in ``handler``).
# ``routeKind`` is prefixed ``client_`` so a client op is never confused with the inbound
# server route of the same field, while still joining on ``(framework, endpoint)``.

# Only operation keywords are lowercase in GraphQL (``query``/``mutation``); ``type Query``
# carries a capital Q, so this prefilter never fires on SDL. Operation vs SDL are also
# disjoint by node type (operation_definition vs object_type_definition), so the SDL and
# client passes over the same fragment never double-count.
_OP_MARKERS = (b"query", b"mutation", b"subscription")
_GQL_TAGS = {"gql", "graphql"}

_INTERP_RE = re.compile(rb"\$\{[^}]*\}")


def _blank_interpolations(frag: bytes) -> bytes:
    """Replace ``${…}`` template substitutions with equal-length filler so the ``graphql``
    grammar sees valid tokens instead of a hole (``items { ${x} }`` -> ``items { ___ }``).
    Length- and newline-preserving so re-parsed node offsets still map back to TS rows/cols."""
    def repl(m: re.Match[bytes]) -> bytes:
        return bytes(b if b == 0x0A else 0x5F for b in m.group(0))  # keep '\n', else '_'
    return _INTERP_RE.sub(repl, frag)


def _is_gql_tagged(tmpl: Node, source: bytes) -> bool:
    """True if this ``template_string`` is a ``gql``/``graphql`` tagged template —
    ``template_string`` -> ``call_expression`` whose function is the tag identifier. Guards
    against plain template strings that merely contain the word ``query`` (a log message)."""
    call = tmpl.parent
    if call is None or call.type != "call_expression":
        return False
    fn = call.child_by_field_name("function")
    return fn is not None and node_text(fn, source).rsplit(".", 1)[-1] in _GQL_TAGS


def _root_fields(op_def: Node) -> list[Node]:
    """Root selection-set fields of an operation — the actual API operations invoked.
    Skips bare fragment spreads (``...Foo``) at the root, which carry no field name."""
    sel_set = _child(op_def, "selection_set")
    if sel_set is None:
        return []
    fields: list[Node] = []
    for selection in sel_set.named_children:
        if selection.type != "selection":
            continue
        field = _child(selection, "field")
        if field is not None and _child(field, "name") is not None:
            fields.append(field)
    return fields


def _op_request_dto(op_def: Node, sdl: bytes) -> str | None:
    """DTO from the operation's variable definitions — the ``$input``/``$data`` variable if
    present, else the sole variable — as its base type name. ``None`` if 0 or many unnamed."""
    var_defs = _child(op_def, "variable_definitions")
    if var_defs is None:
        return None
    variables = [c for c in var_defs.named_children if c.type == "variable_definition"]
    if not variables:
        return None

    def var_name(v: Node) -> str | None:
        var = _child(v, "variable")
        n = _child(var, "name") if var is not None else None
        return node_text(n, sdl) if n is not None else None

    chosen = next((v for v in variables if var_name(v) in ("input", "data")), None)
    if chosen is None:
        if len(variables) != 1:
            return None
        chosen = variables[0]
    return _base_type_name(_child(chosen, "type"), sdl)


def _emit_client_ops(op_def: Node, kind: str, sdl: bytes, row_base: int, col_base: int,
                     path: str, seen: set[str], routes: list[Statement]) -> None:
    op_name_node = _child(op_def, "name")
    op_name = node_text(op_name_node, sdl) if op_name_node is not None else None
    request_dto = _op_request_dto(op_def, sdl)
    for field in _root_fields(op_def):
        name = _child(field, "name")
        line = row_base + name.start_point[0] + 1
        # Column only shifts on the template's first row (body starts after the backtick);
        # later rows begin at column 0 within the body.
        col = name.start_point[1] + (col_base if name.start_point[0] == 0 else 0)
        routes.append(Statement(
            id=disambiguate(statement_id(path, line, col), seen),
            parentId=file_id(path),
            nodeType="synthetic",
            semanticType="route",
            text=first_line(node_text(field, sdl))[:120],
            method=kind.upper(),
            # endpoint = invoked API field (joins to a server route); operation name is the
            # client-side label, kept in handler.
            endpoint=node_text(name, source=sdl),
            framework="graphql",
            routeKind=f"client_{kind}",
            handler=op_name,
            requestDTO=request_dto,
            startLine=line,
            endLine=row_base + field.end_point[0] + 1,
            path=path,
        ))


def _detect_client_ops(root: Node, source: bytes, path: str, seen: set[str],
                       timeout_micros: int) -> list[Statement]:
    routes: list[Statement] = []

    def walk(n: Node) -> None:
        # Walk whole ``template_string`` nodes, NOT individual ``string_fragment`` children:
        # a ``${…}`` substitution inside the operation body splits the template into several
        # fragments, none of which is a complete GraphQL document. We reconstruct the full
        # document from the template's inner span (between the backticks) and blank the
        # ``${…}`` regions so the interpolated document parses as one operation.
        if n.type == "template_string" and _is_gql_tagged(n, source):
            # Inner span: strip the enclosing backticks (first/last byte of the node).
            inner_start, inner_end = n.start_byte + 1, n.end_byte - 1
            body = source[inner_start:inner_end]
            if any(m in body for m in _OP_MARKERS):
                body = _blank_interpolations(body)
                # row/col base maps GraphQL node offsets back to TS: the inner span begins one
                # column after the opening backtick, on the template's start row.
                row_base = n.start_point[0]
                col_base = n.start_point[1] + 1
                gql_root = parse_source("graphql", body, timeout_micros).root_node

                def gwalk(g: Node) -> None:
                    if g.type == "operation_definition":
                        ot = _child(g, "operation_type")
                        # operation_type may be omitted for a shorthand anonymous query (``{ … }``)
                        kind = _OP_KINDS.get(node_text(ot, body)) if ot is not None else "query"
                        if kind is not None:
                            _emit_client_ops(g, kind, body, row_base, col_base, path, seen, routes)
                    for c in g.named_children:
                        gwalk(c)

                gwalk(gql_root)
        for c in n.named_children:
            walk(c)

    walk(root)
    return routes


# ---- entry point ------------------------------------------------------------

def detect_graphql(root: Node, source: bytes, path: str, *, seen_ids: set[str],
                   timeout_micros: int = 0) -> list[Statement]:
    """Server-side GraphQL routes — resolver-map operations (with handlers) and SDL operations
    (with DTOs). ``timeout_micros`` bounds the secondary ``graphql`` parse of embedded SDL,
    threaded from ``ctx.parse_timeout_micros`` like every other ``parse_source`` call. Returns
    statements to append to the record. (Client operations are handled additively for every TS
    file by :func:`detect_graphql_client`, not here — GraphQLParser owns only server files.)"""
    routes = _detect_resolver_maps(root, source, path, seen_ids)
    routes += _detect_sdl(root, source, path, seen_ids, timeout_micros)
    return routes


# Client ops appear in ANY TS file that imports a gql tag — most of which are owned by the
# base TypeScriptParser (or Angular/NestJS), not GraphQLParser (whose ``claims`` fires only
# on resolver-maps / server SDL). So expose the client pass as an additive detector, run for
# every TS file from ``TypeScriptParser.extract`` (like ``detect_express``/``detect_sdk_calls``).
def detect_graphql_client(root: Node, source: bytes, path: str, record: FileRecord,
                          timeout_micros: int = 0) -> bool:
    """Add client-side GraphQL operation statements (``gql`` tagged templates) to ``record``.
    Returns True if any were found. Cheap byte-guard first so non-GraphQL files skip the walk."""
    if b"gql`" not in source and b"graphql`" not in source:
        return False
    routes = _detect_client_ops(
        root, source, path, {s.id for s in record.statements}, timeout_micros
    )
    record.statements.extend(routes)
    return bool(routes)
