"""Scala class / trait / object / case class / enum extraction → Class + flat methods + statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ClassType, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..statements_common import member_statement
from ..treesitter import line_span, node_text
from .functions import build_function, extract_annotations, modifiers_node
from .statements import extract_statements

_TYPE_NODES = ("type_identifier", "generic_type", "scoped_type_identifier")
_NESTED_CLASS_TYPES = (
    "class_definition",
    "object_definition",
    "trait_definition",
    "enum_definition",
    "package_object",
)


def _is_case(node: Node) -> bool:
    """`case class` / `case object` — the `case` keyword is an anonymous child, so
    named_children cannot see it."""
    return any(c.type == "case" for c in node.children)


def _class_type(node: Node) -> ClassType:
    if node.type == "class_definition":
        return "record" if _is_case(node) else "class"
    if node.type == "trait_definition":
        return "trait"
    # A package object is a static container like a plain object, so it maps the same way.
    if node.type in ("object_definition", "package_object"):
        return "module"
    if node.type == "enum_definition":
        return "enum"
    return "class"


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    extends: str | None = None
    implements: list[str] = []

    extends_clause = next((c for c in node.named_children if c.type == "extends_clause"), None)
    if extends_clause is not None:
        type_nodes = [c for c in extends_clause.named_children if c.type in _TYPE_NODES]
        if type_nodes:
            extends = node_text(type_nodes[0], source)
            implements = [node_text(c, source) for c in type_nodes[1:]]
    return extends, implements


def _constructor_params(node: Node, source: bytes) -> list[ConstructorParam]:
    out: list[ConstructorParam] = []
    # Positional rule: the first `class_parameters` child is the primary constructor.
    first_params = next((c for c in node.named_children if c.type == "class_parameters"), None)
    if first_params is None:
        return out

    for p in first_params.named_children:
        if p.type == "class_parameter":
            nnode = p.child_by_field_name("name")
            tnode = p.child_by_field_name("type")
            out.append(
                ConstructorParam(
                    name=node_text(nnode, source) if nnode is not None else "",
                    type=node_text(tnode, source) if tnode is not None else "",
                )
            )
    return out


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
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)

    modifiers = modifiers_node(node)
    visibility = "public"
    is_abstract = node.type == "trait_definition"
    if modifiers is not None:
        for c in modifiers.children:
            if c.type in ("private", "protected"):
                visibility = c.type
            elif c.type == "abstract":
                is_abstract = True

    tp = node.child_by_field_name("type_parameters")
    generics = node_text(tp, source) if tp is not None else None

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params = _constructor_params(node, source)
    # Members of both a plain `object` and a `package object` are statics.
    is_object = node.type in ("object_definition", "package_object")

    body = node.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(
                body,
                source,
                path,
                parent_id=cid,
                capture=capture,
                limit=limit,
                seen_ids=seen_ids,
            )
        )
        # Walk body members (handling template_body or indented_block inside template_body)
        body_members = body.named_children
        if len(body_members) == 1 and body_members[0].type == "indented_block":
            body_members = body_members[0].named_children

        for member in body_members:
            if member.type in ("function_definition", "function_declaration"):
                fns, fn_statements = build_function(
                    member,
                    source,
                    path,
                    parent_id=cid,
                    class_name=name,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    is_static=is_object,
                    fn_type="method",
                    resolve=resolve,
                )
                methods.extend(fns)
                statements.extend(fn_statements)
            elif member.type in _NESTED_CLASS_TYPES:
                sub_classes, sub_methods, sub_statements = build_class(
                    member,
                    source,
                    path,
                    parent_id=cid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                nested_classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_statements)

    # Scala 3 enum members emitted as flat statements parented to the enum Class
    if capture and node.type == "enum_definition" and body is not None:
        case_nodes: list[Node] = []
        for c in body.named_children:
            if c.type in ("simple_enum_case", "full_enum_case"):
                case_nodes.append(c)
            elif c.type == "enum_case_definitions":
                for cc in c.named_children:
                    if cc.type in ("simple_enum_case", "full_enum_case"):
                        case_nodes.append(cc)
        for cn in case_nodes:
            statements.append(
                member_statement(
                    cn,
                    source,
                    path,
                    parent_id=cid,
                    limit=limit,
                    seen_ids=seen_ids,
                )
            )

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_class_type(node),
        visibility=visibility,
        isAbstract=is_abstract,
        generics=generics,
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        decorators=extract_annotations(node, source),
        startLine=start,
        endLine=end,
    )
    return [cls, *nested_classes], methods, statements
