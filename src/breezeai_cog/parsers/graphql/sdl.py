"""Walk a whole standalone ``.graphql``/``.gql`` document and emit flat ``Statement``s.

Covers every construct the GraphQL grammar produces (verified empirically against the
``graphql`` tree-sitter grammar):

* **Composite types** — ``type`` / ``extend type`` / ``interface`` / ``union`` → a
  ``graphql_entity`` statement (``endpoint`` = the type name, the **full body** carried on
  ``text`` so columns, relations, and any ``@key`` directive survive as text).
* **Root operation types** — ``type Query|Mutation|Subscription`` → one ``route`` statement
  per field (``method``/``endpoint``/``routeKind`` + request/response DTOs).
* **Value shapes** — ``enum`` / ``input`` / ``scalar`` / ``directive`` / ``schema`` → a plain
  statement (real ``nodeType``, ``semanticType`` null), ``endpoint`` = its name so a
  request DTO name joins back to its ``input`` definition.
* **Executable docs** — ``query`` / ``mutation`` / ``subscription`` → an ``api_call`` per
  invoked root field; ``fragment`` → a plain statement anchored to its target type.

Every statement is flat and parented to the file (``parentId`` = ``file_id(path)``); the
entity↔field↔operation linkage is expressed through the ``endpoint`` (type name) join key, not
through statement→statement parenting. Relationships between entities are carried on the
entity's ``text`` and left to the backend relationship phase to materialise (honest-null —
the parser never invents an edge).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text

#: Root operation types — their fields are endpoints (routes), not entity columns.
_ROOT_TYPES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}
_OP_KINDS = {"query": "query", "mutation": "mutation", "subscription": "subscription"}

_COMPOSITE = frozenset(
    {
        "object_type_definition",
        "object_type_extension",
        "interface_type_definition",
        "union_type_definition",
    }
)
_PLAIN = frozenset(
    {
        "enum_type_definition",
        "input_object_type_definition",
        "scalar_type_definition",
        "directive_definition",
        "schema_definition",
    }
)



# ---- small AST helpers ------------------------------------------------------


def _child(node: Node, typ: str) -> Node | None:
    return next((c for c in node.named_children if c.type == typ), None)


def _name(node: Node, src: bytes) -> str | None:
    n = _child(node, "name")
    return node_text(n, src) if n is not None else None


def _base_type_name(node: Node | None, src: bytes) -> str | None:
    """Underlying type name of a ``type`` node, stripping ``!``/``[]`` wrappers:
    ``[Post!]!`` -> ``Post``. Recurses to the first ``named_type``."""
    if node is None:
        return None
    if node.type == "named_type":
        return _name(node, src)
    for c in node.named_children:
        found = _base_type_name(c, src)
        if found is not None:
            return found
    return None


def _selection_fields(op: Node) -> list[Node]:
    """Top-level invoked ``field`` nodes of an operation. The grammar nests them as
    ``selection_set -> selection -> field`` (a bare ``field`` child is also tolerated)."""
    sel = _child(op, "selection_set")
    if sel is None:
        return []
    fields: list[Node] = []
    for child in sel.named_children:
        if child.type == "field":
            fields.append(child)
        elif child.type == "selection":
            f = _child(child, "field")
            if f is not None:
                fields.append(f)
    return fields


def _fragment_name(n: Node, src: bytes) -> str | None:
    """A fragment's name lives under ``fragment_name -> name`` (not a direct ``name`` child)."""
    fn = _child(n, "fragment_name")
    return _name(fn, src) if fn is not None else None


def _request_dto(field: Node, src: bytes) -> str | None:
    """The input DTO of a field's args — the ``input``/``data`` arg if present, else the
    first arg — as its base type name."""
    args = _child(field, "arguments_definition")
    if args is None:
        return None
    inputs = [c for c in args.named_children if c.type == "input_value_definition"]
    if not inputs:
        return None
    chosen = next((i for i in inputs if _name(i, src) in ("input", "data")), inputs[0])
    return _base_type_name(_child(chosen, "type"), src)


# ---- emission ---------------------------------------------------------------


def collect_graphql_statements(
    root: Node, source: bytes, path: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    out: list[Statement] = []
    fid = file_id(path)

    def stmt(node: Node, line: int, col: int, **fields: object) -> Statement:
        return Statement(
            id=disambiguate(statement_id(path, line, col), seen_ids),
            parentId=fid,
            path=path,
            framework="graphql",
            startLine=line,
            endLine=node.end_point[0] + 1,
            **fields,  # type: ignore[arg-type]
        )

    def emit_composite(n: Node) -> None:
        tname = _name(n, source)
        if tname in _ROOT_TYPES:  # root operation type -> its fields are routes
            _emit_root_fields(n, _ROOT_TYPES[tname])
            return
        line, col = n.start_point[0] + 1, n.start_point[1]
        out.append(
            stmt(
                n,
                line,
                col,
                nodeType=n.type,
                semanticType="graphql_entity",
                name=tname,
                endpoint=tname,
                text=node_text(n, source)[:limit],
            )
        )

    def _emit_root_fields(obj: Node, kind: str) -> None:
        fields_def = _child(obj, "fields_definition")
        if fields_def is None:
            return
        for field in fields_def.named_children:
            if field.type != "field_definition":
                continue
            name = _child(field, "name")
            if name is None:
                continue
            line, col = name.start_point[0] + 1, name.start_point[1]
            out.append(
                stmt(
                    field,
                    line,
                    col,
                    nodeType="field_definition",
                    semanticType="route",
                    name=node_text(name, source),
                    text=node_text(field, source)[:limit],
                    method=kind.upper(),
                    endpoint=node_text(name, source),
                    routeKind=kind,
                    requestDTO=_request_dto(field, source),
                    responseDTO=_base_type_name(_child(field, "type"), source),
                )
            )

    def emit_plain(n: Node) -> None:
        line, col = n.start_point[0] + 1, n.start_point[1]
        name = _name(n, source)
        out.append(
            stmt(
                n,
                line,
                col,
                nodeType=n.type,
                name=name,
                endpoint=name,
                text=node_text(n, source)[:limit],
            )
        )

    def emit_operation(n: Node) -> None:
        ot = _child(n, "operation_type")
        kind = _OP_KINDS.get(node_text(ot, source)) if ot is not None else "query"
        op_name = _name(n, source)
        fields = _selection_fields(n)
        method = (kind or "query").upper()
        if not fields:
            line, col = n.start_point[0] + 1, n.start_point[1]
            out.append(
                stmt(
                    n,
                    line,
                    col,
                    nodeType="operation_definition",
                    semanticType="api_call",
                    name=op_name,
                    endpoint=op_name,
                    method=method,
                    routeKind=kind,
                    handler=op_name,
                    text=node_text(n, source)[:limit],
                )
            )
            return
        for field in fields:
            name = _child(field, "name")
            if name is None:
                continue
            line, col = name.start_point[0] + 1, name.start_point[1]
            out.append(
                stmt(
                    field,
                    line,
                    col,
                    nodeType="field",
                    semanticType="api_call",
                    name=op_name,
                    endpoint=node_text(name, source),  # invoked server field (joins to route)
                    method=method,
                    routeKind=kind,
                    handler=op_name,
                    text=node_text(field, source)[:limit],
                )
            )

    def emit_fragment(n: Node) -> None:
        cond = _child(n, "type_condition")
        target = _base_type_name(cond, source) if cond is not None else None
        line, col = n.start_point[0] + 1, n.start_point[1]
        out.append(
            stmt(
                n,
                line,
                col,
                nodeType="fragment_definition",
                name=_fragment_name(n, source),
                endpoint=target,
                text=node_text(n, source)[:limit],
            )
        )

    def walk(n: Node) -> None:
        t = n.type
        if t in _COMPOSITE:
            emit_composite(n)
            return
        if t in _PLAIN:
            emit_plain(n)
            return
        if t == "operation_definition":
            emit_operation(n)
            return
        if t == "fragment_definition":
            emit_fragment(n)
            return
        for c in n.named_children:
            walk(c)

    walk(root)
    return out
