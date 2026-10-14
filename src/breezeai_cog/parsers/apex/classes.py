"""Apex class / interface / enum extraction → Class + flat methods + statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..statements_common import emit_enum_members
from ..treesitter import line_span, node_text
from .functions import build_method, extract_params
from .mappings import TYPE_DECLARATIONS
from .modifiers import (
    declared_name,
    extract_annotations,
    modifier_keywords,
    modifiers_node,
    sharing_of,
    visibility_of,
)
from .statements import extract_statements

_TYPE_NODES = ("type_identifier", "scoped_type_identifier", "generic_type")


def _type_list(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    type_list = next((c for c in node.named_children if c.type == "type_list"), node)
    return [node_text(c, source) for c in type_list.named_children if c.type in _TYPE_NODES]


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """``(extends, implements)``. An interface's parents (``extends_interfaces``) are
    reported as ``implements`` — the same list a class uses for its interfaces."""
    superclass = node.child_by_field_name("superclass")
    parent = next((c for c in superclass.named_children if c.type in _TYPE_NODES), None) \
        if superclass is not None else None
    extends = node_text(parent, source) if parent is not None else None
    implements = _type_list(node.child_by_field_name("interfaces"), source)
    if node.type == "interface_declaration":
        implements = _type_list(
            next((c for c in node.named_children if c.type == "extends_interfaces"), None), source
        )
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
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Return ``(classes, methods, statements)`` — flat, linked by parentId. The class list
    is this type plus any nested types, each parented to its enclosing type."""
    name = declared_name(node, source)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)
    modifiers = modifiers_node(node)
    keywords = modifier_keywords(modifiers)
    is_interface = node.type == "interface_declaration"
    sharing = sharing_of(keywords)

    methods: list[Function] = []
    statements: list[Statement] = []
    nested: list[Class] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture,
                               limit=limit, seen_ids=seen_ids)
        )
        for member in body.named_children:
            if member.type in ("method_declaration", "constructor_declaration"):
                fn, fn_statements = build_method(
                    member, source, path, parent_id=cid, class_name=name,
                    in_interface=is_interface, seen_ids=seen_ids, capture=capture,
                    limit=limit, resolve=resolve,
                )
                methods.append(fn)
                statements.extend(fn_statements)
                if member.type == "constructor_declaration":
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(member.child_by_field_name("parameters"), source)
                    ]
            elif member.type in TYPE_DECLARATIONS:
                sub_classes, sub_methods, sub_statements = build_class(
                    member, source, path, parent_id=cid, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                nested.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_statements)

    if capture and node.type == "enum_declaration" and body is not None:
        statements.extend(
            emit_enum_members(
                body, source, path, member_types={"enum_constant"},
                parent_id=cid, limit=limit, seen_ids=seen_ids,
            )
        )

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=TYPE_DECLARATIONS[node.type],  # type: ignore[arg-type]
        visibility=visibility_of(keywords),
        isAbstract=is_interface or "abstract" in keywords,
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        decorators=extract_annotations(modifiers, source),
        metadata={"sharing": sharing} if sharing else None,
        startLine=start,
        endLine=end,
    )
    return [cls, *nested], methods, statements
