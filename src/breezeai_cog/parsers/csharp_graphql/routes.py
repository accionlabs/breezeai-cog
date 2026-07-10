"""graphql-dotnet (code-first) route detection.

graphql-dotnet declares its API surface with **schema types** — classes deriving from
``ObjectGraphType`` whose constructor calls ``Field<T>("name")`` / ``FieldAsync<T>("name")``
to declare fields. Only a small subset of these are *operations* (the query/mutation/
subscription entry points a client can call); the rest are **output/data types** (``…Type``
classes) and **result wrappers** (``ObjectGraphType<T>``) whose fields are data shape, not
endpoints. Emitting every ``Field()`` would over-capture ~9× (measured on a real repo:
4,835 ``Field`` calls vs ~520 operations).

Two signals — matching how the runtime is wired, and grounded on the real
``ProductCatalogue`` schema — separate operations from data:

* **base type** — an operation root/namespace derives from the **non-generic**
  ``ObjectGraphType``. ``ObjectGraphType<T>`` (generic) is a result/data wrapper → excluded.
* **name convention** — the class is a root (``Query``/``Mutation``/``Subscription``) or a
  namespace group ending in one of those (``ProductQuery``, ``AdminMutations``, …). This is
  the same convention the schema wiring relies on; the framework itself is name-agnostic, so
  a root registered under an off-convention class name is a known blind spot (honest gap, not
  a wrong route — see the task notes). HotChocolate (attribute-based) is not handled here.

Emits ``semanticType="route"``, ``framework="graphql"``, ``routeKind ∈ {query, mutation,
subscription}``, ``method`` = the upper-cased kind, ``endpoint``/``handler`` = the operation
name — mirroring the TypeScript GraphQL detector so the backend joins them uniformly.
"""

from __future__ import annotations

from typing import Any, Iterator

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import first_line, node_text

_FIELD_CALLS = {"Field", "FieldAsync"}      # name is the first positional string arg
_ADDFIELD_CALLS = {"AddField"}              # name is a `Name = "…"` object-initializer entry
_NESTED_TYPES = ("class_declaration", "struct_declaration", "record_declaration")


def _name_kind(name: str) -> str | None:
    """The operation kind implied by an operation-type class name, or None. Checked
    subscription→mutation→query so the plural/singular suffixes never collide."""
    for suffix, kind in (
        ("Subscription", "subscription"), ("Subscriptions", "subscription"),
        ("Mutation", "mutation"), ("Mutations", "mutation"),
        ("Query", "query"), ("Queries", "query"),
    ):
        if name.endswith(suffix):
            return kind
    return None


def _operation_kind(cls: Any) -> str | None:
    """Operation kind if ``cls`` is a graphql-dotnet operation type (non-generic
    ``ObjectGraphType`` whose name follows the query/mutation/subscription convention),
    else None. A generic base (``ObjectGraphType<T>``) is a result/data wrapper → excluded."""
    ext = cls.extends or ""
    if "<" in ext:  # ObjectGraphType<T> — a data/result type, not an operation root
        return None
    if ext.rsplit(".", 1)[-1] != "ObjectGraphType":
        return None
    return _name_kind(cls.name)


def _bare_call_name(func: Node, source: bytes) -> str | None:
    """The method name of an invocation's function node — the identifier of a
    ``generic_name`` (``Field<T>``) or a plain ``identifier`` (``Field``). A member access
    (``x.Resolve``) is a chained builder method, not a field declaration → None; this is why
    the builder chain ``Field<T>("x").Resolve(…)`` matches only the inner ``Field`` call."""
    if func.type == "generic_name":
        ident = next((c for c in func.named_children if c.type == "identifier"), None)
        return node_text(ident, source) if ident is not None else None
    if func.type == "identifier":
        return node_text(func, source)
    return None


def _first_string_arg(call: Node, source: bytes) -> str | None:
    """The field name — the first ``string_literal`` positional argument of a ``Field``
    call. None when the field is declared expression-only (``Field(x => x.Foo)``): the name
    is inferred by the runtime and we do not fabricate it (honest-null)."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        lit = arg.named_children[0] if arg.type == "argument" and arg.named_children else arg
        if lit is not None and lit.type == "string_literal":
            return _literal_text(lit, source)
    return None


def _initializer_name(call: Node, source: bytes) -> str | None:
    """The field name of an ``AddField(new FieldType { Name = "…" })`` call — the string
    value of the ``Name`` assignment in the object initializer. None if absent (honest-null)."""
    args = call.child_by_field_name("arguments")
    for arg in (args.named_children if args is not None else []):
        obj = arg.named_children[0] if arg.type == "argument" and arg.named_children else arg
        if obj is None or obj.type != "object_creation_expression":
            continue
        init = next((c for c in obj.named_children if c.type == "initializer_expression"), None)
        for assign in (init.named_children if init is not None else []):
            if assign.type != "assignment_expression":
                continue
            left = assign.child_by_field_name("left") or (assign.named_children[0] if assign.named_children else None)
            right = assign.child_by_field_name("right") or (assign.named_children[-1] if assign.named_children else None)
            if (left is not None and node_text(left, source) == "Name"
                    and right is not None and right.type == "string_literal"):
                return _literal_text(right, source)
    return None


def _literal_text(lit: Node, source: bytes) -> str:
    content = next((c for c in lit.named_children if c.type == "string_literal_content"), None)
    return node_text(content, source) if content is not None else node_text(lit, source).strip('"')


def _field_operations(class_node: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """Yield ``(operation_name, call_node)`` for each named ``Field``/``FieldAsync`` declared
    directly in ``class_node``. Does not descend into a nested type declaration (handled as
    its own class); skips expression-only fields carrying no string name."""
    def walk(node: Node) -> Iterator[tuple[str, Node]]:
        for child in node.named_children:
            if child.type in _NESTED_TYPES:
                continue
            if child.type == "invocation_expression":
                func = child.child_by_field_name("function")
                nm = _bare_call_name(func, source) if func is not None else None
                if nm in _FIELD_CALLS:
                    op = _first_string_arg(child, source)
                elif nm in _ADDFIELD_CALLS:
                    op = _initializer_name(child, source)
                else:
                    op = None
                if op:
                    yield op, child
            yield from walk(child)
    yield from walk(class_node)


def detect_graphql_dotnet_routes(
    record: Any, root: Node, source: bytes, path: str, seen: set[str]
) -> list[Statement]:
    op_types = {cls.name: (cls.id, kind) for cls in record.classes
                if (kind := _operation_kind(cls)) is not None}
    if not op_types:
        return []

    routes: list[Statement] = []

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == "class_declaration":
                name_node = child.child_by_field_name("name")
                name = node_text(name_node, source) if name_node is not None else None
                info = op_types.get(name) if name is not None else None
                if info is not None:
                    _emit(child, info, source, path, seen, routes)
                    continue  # its Field calls are consumed here — don't recurse into it
            visit(child)

    visit(root)
    return routes


def _emit(class_node: Node, info: tuple[str, str], source: bytes, path: str,
          seen: set[str], routes: list[Statement]) -> None:
    parent_id, kind = info
    for op, node in _field_operations(class_node, source):
        sl, sc = node.start_point[0] + 1, node.start_point[1]
        routes.append(Statement(
            id=disambiguate(statement_id(path, sl, sc), seen),
            parentId=parent_id,
            nodeType="invocation_expression",
            semanticType="route",
            text=first_line(node_text(node, source))[:120],
            method=kind.upper(),
            endpoint=op,
            framework="graphql",
            handler=op,
            handlerLine=sl,
            routeKind=kind,
            isRegex=False,
            startLine=sl,
            endLine=node.end_point[0] + 1,
            path=path,
        ))
