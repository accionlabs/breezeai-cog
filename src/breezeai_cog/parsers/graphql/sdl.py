"""Shared GraphQL SDL / operation extractor — used by both the standalone ``.graphql``
language parser and the embedded ``gql`…`` path in ``typescript_graphql`` (so the two emit
the same nodes; the only difference is ``nodeType`` = real grammar node for a standalone file
vs the ``synthetic`` sentinel for an embedded template, set via ``synthetic``).

The type **system** is structure → ``Class`` nodes (matching every other language: a GraphQL
``interface`` and a Java ``interface`` are both ``Class type="interface"``); operations are
behaviour → ``Statement``s. Node types verified empirically against the ``graphql`` grammar.

| Construct | Emitted as |
|-----------|------------|
| ``type`` (non-root) / ``interface`` / ``enum`` / ``union`` / ``input`` (+ ``extend`` of each) | ``Class`` (class/interface/enum/union/record) |
| ``type X @key(fields:…)`` | the Class **plus** a ``graphql_entity`` statement with ``keyFields`` |
| object/interface/input fields, enum values, union members | child ``Statement``s parented to the Class |
| ``type Query/Mutation/Subscription`` (or a ``schema{}``-declared root) field | ``route`` statement |
| ``scalar`` / ``directive`` / ``schema`` | plain ``Statement`` (name + text) |
| ``query/mutation/subscription`` op / ``fragment`` | ``api_call`` / plain statement |

Fields carry their full source on ``text``; relation targets (a field's type) and field-level
detail (args, defaults, cardinality) live in that text — structuring them is deferred. Entity
relationships are left to the backend relationship phase (honest-null — no invented edges).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate, file_id, statement_id
from ...schemas import Class, Decorator, Statement
from ..treesitter import node_text

# Conventional root operation types; a ``schema{}`` block can add/override (custom names).
_ROOT_DEFAULT = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}
_OP_KINDS = {"query": "query", "mutation": "mutation", "subscription": "subscription"}

# Type-system constructs → ClassType (definitions and their ``extend`` counterparts).
_TYPE_KINDS = {
    "object_type_definition": "class",
    "object_type_extension": "class",
    "interface_type_definition": "interface",
    "interface_type_extension": "interface",
    "enum_type_definition": "enum",
    "enum_type_extension": "enum",
    "union_type_definition": "union",
    "union_type_extension": "union",
    "input_object_type_definition": "record",
    "input_object_type_extension": "record",
}
# Captured as plain statements (name + text), no semanticType — they are neither behaviour
# nor a type-with-members. (Kept, not dropped: a custom scalar is a real relation target and
# a ``schema{}`` block carries custom root names.)
_PLAIN = frozenset(
    {
        "scalar_type_definition",
        "scalar_type_extension",
        "directive_definition",
        "schema_definition",
        "schema_extension",
    }
)


# ---- small AST helpers ------------------------------------------------------


def _child(node: Node, typ: str) -> Node | None:
    return next((c for c in node.named_children if c.type == typ), None)


def _name(node: Node, src: bytes) -> str | None:
    n = _child(node, "name")
    return node_text(n, src) if n is not None else None


def _deep_name(node: Node, src: bytes) -> str | None:
    """First ``name`` descendant's text (for nodes whose name is nested, e.g. an
    ``enum_value_definition`` -> ``enum_value`` -> ``name``)."""
    stack = list(node.named_children)
    while stack:
        n = stack.pop(0)
        if n.type == "name":
            return node_text(n, src)
        stack.extend(n.named_children)
    return None


def _base_type_name(node: Node | None, src: bytes) -> str | None:
    """Underlying type name, stripping ``!``/``[]`` wrappers (``[Post!]!`` -> ``Post``)."""
    if node is None:
        return None
    if node.type == "named_type":
        return _name(node, src)
    for c in node.named_children:
        found = _base_type_name(c, src)
        if found is not None:
            return found
    return None


def _named_types(node: Node, src: bytes) -> list[str]:
    """All ``named_type`` names under ``node`` (union members, implemented interfaces)."""
    out: list[str] = []
    stack = list(node.named_children)
    while stack:
        n = stack.pop(0)
        if n.type == "named_type":
            nm = _name(n, src)
            if nm:
                out.append(nm)
        else:
            stack.extend(n.named_children)
    return out


def _string_arg(directive: Node, arg_name: str, src: bytes) -> str | None:
    """The string value of a directive argument (``@key(fields: "id")`` -> ``id``)."""
    args = _child(directive, "arguments")
    if args is None:
        return None
    for arg in args.named_children:
        if arg.type != "argument" or _name(arg, src) != arg_name:
            continue
        value = _child(arg, "value")
        return node_text(value, src).strip('"').strip() if value is not None else None
    return None


def parse_key_selection(raw: str) -> list[str]:
    """Top-level field names of a Federation ``@key`` selection. Handles ``fields: "id org"``
    (unbraced) and ``selectionSet: "{ id org }"`` (brace-wrapped), dropping nested
    sub-selections — ``id org { id }`` -> ``["id","org"]`` — via brace-depth tracking so an
    inner ``{`` never leaks in as a stray token (BREEZEAI-690)."""
    base = 1 if raw.lstrip().startswith("{") else 0
    fields: list[str] = []
    depth = 0
    token = ""
    for ch in raw:
        if ch in "{}":
            if depth == base and token.strip():
                fields.append(token.strip())
            token = ""
            depth += 1 if ch == "{" else -1
        elif ch in ", \t\n":
            if depth == base and token.strip():
                fields.append(token.strip())
            token = ""
        else:
            token += ch
    if depth == base and token.strip():
        fields.append(token.strip())
    return fields


def _key_fields(node: Node, src: bytes) -> list[str] | None:
    """Key field names from a type's ``@key`` directive, or None if it has no ``@key``."""
    directives = _child(node, "directives")
    if directives is None:
        return None
    key = next(
        (d for d in directives.named_children if d.type == "directive" and _name(d, src) == "key"),
        None,
    )
    if key is None:
        return None
    raw = _string_arg(key, "fields", src) or _string_arg(key, "selectionSet", src)
    return parse_key_selection(raw) if raw is not None else []


def _directives(
    node: Node, src: bytes, *, exclude: frozenset[str] = frozenset()
) -> list[Decorator]:
    """Applied directives as ``{name, args}`` decorators (``@auth(role: ADMIN)`` -> name
    ``auth``, args ``["role: ADMIN"]``). ``exclude`` skips names handled elsewhere (``key``)."""
    d = _child(node, "directives")
    if d is None:
        return []
    out: list[Decorator] = []
    for directive in d.named_children:
        if directive.type != "directive":
            continue
        nm = _name(directive, src)
        if nm is None or nm in exclude:
            continue
        args_node = _child(directive, "arguments")
        args = (
            [node_text(a, src) for a in args_node.named_children if a.type == "argument"]
            if args_node is not None
            else []
        )
        out.append(Decorator(name=nm, args=args))
    return out


def _implements(node: Node, src: bytes) -> list[str]:
    ii = _child(node, "implements_interfaces")
    return _named_types(ii, src) if ii is not None else []


def _selection_fields(op: Node) -> list[Node]:
    """Top-level invoked ``field`` nodes of an operation (``selection_set -> selection ->
    field``; a bare ``field`` child is also tolerated)."""
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
    fn = _child(n, "fragment_name")
    return _name(fn, src) if fn is not None else None


def _request_dto(field: Node, src: bytes) -> str | None:
    """A root field's request DTO — the ``input``/``data`` arg (else the first) base type."""
    args = _child(field, "arguments_definition")
    if args is None:
        return None
    inputs = [c for c in args.named_children if c.type == "input_value_definition"]
    if not inputs:
        return None
    chosen = next((i for i in inputs if _name(i, src) in ("input", "data")), inputs[0])
    return _base_type_name(_child(chosen, "type"), src)


def _resolve_roots(root: Node, src: bytes) -> dict[str, str]:
    """Type name -> operation kind for every root operation type: the conventional
    ``Query``/``Mutation``/``Subscription`` plus any declared in a ``schema{}`` block (which is
    how a schema binds custom-named roots, e.g. ``schema { query: RootQuery }``)."""
    roots = dict(_ROOT_DEFAULT)
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in ("schema_definition", "schema_extension"):
            for rot in n.named_children:
                if rot.type != "root_operation_type_definition":
                    continue
                ot = _child(rot, "operation_type")
                nt = _child(rot, "named_type")
                kind = _OP_KINDS.get(node_text(ot, src)) if ot is not None else None
                tname = _name(nt, src) if nt is not None else None
                if kind and tname:
                    roots[tname] = kind
        stack.extend(n.named_children)
    return roots


# ---- extraction -------------------------------------------------------------


def extract_graphql(
    root: Node,
    source: bytes,
    path: str,
    *,
    seen_ids: set[str],
    limit: int,
    row_offset: int = 0,
    col_offset: int = 0,
    synthetic: bool = False,
) -> tuple[list[Class], list[Statement]]:
    """Walk a parsed GraphQL document → ``(classes, statements)``. ``row_offset``/``col_offset``
    map line/col back to the host file when the document was re-parsed out of a ``gql`` template
    (0 for a standalone file); ``synthetic`` sets ``nodeType="synthetic"`` on emitted statements
    for that embedded case."""
    classes: list[Class] = []
    statements: list[Statement] = []
    fid = file_id(path)
    roots = _resolve_roots(root, source)
    # One Class per type name per file: a definition emits the Class; an ``extend`` of the same
    # type (or a second declaration) reuses it and only adds members, so def + extension form a
    # single node rather than duplicates.
    emitted_class: dict[str, str] = {}

    def line_of(n: Node) -> int:
        return n.start_point[0] + 1 + row_offset

    def col_of(n: Node) -> int:
        return n.start_point[1] + (col_offset if n.start_point[0] == 0 else 0)

    def nt(real: str) -> str:
        return "synthetic" if synthetic else real

    def mk(node: Node, parent_id: str, **fields: object) -> Statement:
        return Statement(
            id=disambiguate(statement_id(path, line_of(node), col_of(node)), seen_ids),
            parentId=parent_id,
            path=path,
            framework="graphql",
            startLine=line_of(node),
            endLine=node.end_point[0] + 1 + row_offset,
            **fields,  # type: ignore[arg-type]
        )

    def emit_root_fields(obj: Node, kind: str) -> None:
        fields_def = _child(obj, "fields_definition")
        if fields_def is None:
            return
        for field in fields_def.named_children:
            if field.type != "field_definition":
                continue
            name = _name(field, source)
            if name is None:
                continue
            statements.append(
                mk(
                    field,
                    fid,
                    nodeType=nt("field_definition"),
                    semanticType="route",
                    name=name,
                    text=node_text(field, source)[:limit],
                    method=kind.upper(),
                    endpoint=name,
                    routeKind=kind,
                    requestDTO=_request_dto(field, source),
                    responseDTO=_base_type_name(_child(field, "type"), source),
                )
            )

    def emit_members(n: Node, classtype: str, cid: str) -> None:
        if classtype == "enum":
            vals = _child(n, "enum_values_definition")
            for v in vals.named_children if vals is not None else []:
                if v.type != "enum_value_definition":
                    continue
                statements.append(
                    mk(
                        v,
                        cid,
                        nodeType=nt("enum_value_definition"),
                        name=_deep_name(v, source),
                        text=node_text(v, source)[:limit],
                        decorators=_directives(v, source),
                    )
                )
            return
        if classtype == "union":
            umt = _child(n, "union_member_types")
            if umt is None:
                return
            # _named_types walks the left-recursive union_member_types nesting for all members
            for name in _named_types(umt, source):
                statements.append(
                    Statement(
                        id=disambiguate(statement_id(path, line_of(umt), col_of(umt)), seen_ids),
                        parentId=cid,
                        path=path,
                        framework="graphql",
                        nodeType=nt("named_type"),
                        name=name,  # the member type — a relation to it
                        text=name,
                        startLine=line_of(umt),
                        endLine=umt.end_point[0] + 1 + row_offset,
                    )
                )
            return
        # object / interface / input fields
        fields_def = _child(n, "fields_definition") or _child(n, "input_fields_definition")
        for f in fields_def.named_children if fields_def is not None else []:
            if f.type not in ("field_definition", "input_value_definition"):
                continue
            statements.append(
                mk(
                    f,
                    cid,
                    nodeType=nt(f.type),
                    name=_name(f, source),
                    text=node_text(f, source)[:limit],
                    decorators=_directives(f, source),
                )
            )

    def emit_type(n: Node, classtype: str) -> None:
        name = _name(n, source)
        if name is None:
            return
        if n.type in ("object_type_definition", "object_type_extension") and name in roots:
            emit_root_fields(n, roots[name])  # a root operation type: fields are routes
            return
        cid = emitted_class.get(name)
        if cid is None:  # first declaration of this type in the file → emit the Class node
            cid = disambiguate(class_id(path, name), seen_ids)
            emitted_class[name] = cid
            classes.append(
                Class(
                    id=cid,
                    parentId=fid,
                    name=name,
                    type=classtype,  # type: ignore[arg-type]
                    path=path,
                    startLine=line_of(n),
                    endLine=n.end_point[0] + 1 + row_offset,
                    implements=_implements(n, source),
                    decorators=_directives(n, source, exclude=frozenset({"key"})),
                )
            )
        key = _key_fields(n, source)
        if key is not None:  # federated entity — the @key marker, as a statement child
            statements.append(
                mk(
                    n,
                    cid,
                    nodeType=nt(n.type),
                    semanticType="graphql_entity",
                    name=name,
                    endpoint=name,
                    keyFields=key,
                    text=node_text(n, source)[:limit],
                )
            )
        emit_members(n, classtype, cid)

    def emit_plain(n: Node) -> None:
        name = _name(n, source)
        statements.append(
            mk(
                n,
                fid,
                nodeType=nt(n.type),
                name=name,
                endpoint=name,
                text=node_text(n, source)[:limit],
            )
        )

    def emit_operation(n: Node) -> None:
        ot = _child(n, "operation_type")
        kind = _OP_KINDS.get(node_text(ot, source)) if ot is not None else "query"
        op_name = _name(n, source)
        method = (kind or "query").upper()
        fields = _selection_fields(n)
        if not fields:
            statements.append(
                mk(
                    n,
                    fid,
                    nodeType=nt("operation_definition"),
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
            name = _name(field, source)
            if name is None:
                continue
            statements.append(
                mk(
                    field,
                    fid,
                    nodeType=nt("field"),
                    semanticType="api_call",
                    name=op_name,
                    endpoint=name,  # invoked server field (joins to a route)
                    method=method,
                    routeKind=kind,
                    handler=op_name,
                    text=node_text(field, source)[:limit],
                )
            )

    def emit_fragment(n: Node) -> None:
        cond = _child(n, "type_condition")
        statements.append(
            mk(
                n,
                fid,
                nodeType=nt("fragment_definition"),
                name=_fragment_name(n, source),
                endpoint=_base_type_name(cond, source) if cond is not None else None,
                text=node_text(n, source)[:limit],
            )
        )

    def walk(n: Node) -> None:
        classtype = _TYPE_KINDS.get(n.type)
        if classtype is not None:
            emit_type(n, classtype)
            return
        if n.type in _PLAIN:
            emit_plain(n)
            return
        if n.type == "operation_definition":
            emit_operation(n)
            return
        if n.type == "fragment_definition":
            emit_fragment(n)
            return
        for c in n.named_children:
            walk(c)

    walk(root)
    return classes, statements
