"""Java class / interface / enum / record extraction → Class + flat methods + statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..treesitter import line_span, node_text
from .functions import build_method, extract_annotations, extract_params, modifiers_node
from .statements import extract_statements

_TYPE = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
}
_TYPE_NODES = ("type_identifier", "scoped_type_identifier", "generic_type")


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    extends: str | None = None
    superclass = node.child_by_field_name("superclass")
    if superclass is not None:
        ti = next((c for c in superclass.named_children if c.type in _TYPE_NODES), None)
        extends = node_text(ti, source) if ti is not None else None
    implements: list[str] = []
    interfaces = node.child_by_field_name("interfaces")
    if interfaces is not None:
        type_list = next((c for c in interfaces.named_children if c.type == "type_list"), interfaces)
        implements = [node_text(c, source) for c in type_list.named_children if c.type in _TYPE_NODES]
    return extends, implements


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
) -> tuple[Class, list[Function], list[Statement]]:
    name = node_text(node.child_by_field_name("name"), source)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)

    methods: list[Function] = []
    statements: list[Statement] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        for member in body.named_children:
            if member.type in ("method_declaration", "constructor_declaration"):
                fn, fn_statements = build_method(
                    member, source, path,
                    parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                )
                methods.append(fn)
                statements.extend(fn_statements)
                if member.type == "constructor_declaration":
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(member.child_by_field_name("parameters"), source)
                    ]

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_TYPE.get(node.type, "class"),
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        decorators=extract_annotations(modifiers_node(node), source),
        startLine=start,
        endLine=end,
    )
    return cls, methods, statements
