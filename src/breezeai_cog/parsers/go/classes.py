"""Go type extraction for struct/interface declarations."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate, function_id
from ...schemas import Class, Function, Statement
from ..treesitter import line_span, node_text
from .functions import _extract_params, _result_type


def _kind(node: Node, source: bytes) -> str:
    type_node = next((c for c in node.named_children if c.type in {"struct_type", "interface_type"}), None)
    if type_node is not None:
        return "struct" if type_node.type == "struct_type" else "interface"
    return "class"


def _embedded_type(spec: Node, source: bytes) -> str | None:
    type_node = spec.child_by_field_name("type")
    if type_node is None or type_node.type != "struct_type":
        return None
    field_list = next(
        (child for child in type_node.named_children if child.type == "field_declaration_list"),
        None,
    )
    fields = [child for child in field_list.named_children if child.type == "field_declaration"] if field_list else []
    embedded: list[str] = []
    for field in fields:
        names = field.child_by_field_name("name")
        field_type = field.child_by_field_name("type")
        if names is None and field_type is not None and field_type.type in {"type_identifier", "selector_type", "pointer_type"}:
            text = node_text(field_type, source).lstrip("*").rsplit(".", 1)[-1]
            embedded.append(text)
    return embedded[0] if len(embedded) == 1 else None


def _interface_methods(spec: Node, source: bytes, path: str, *, parent_id: str,
                       seen_ids: set[str]) -> list[Function]:
    """The method set of an interface (`Get(id ID) (*User, error)`), one Function per
    `method_elem`, parented to the interface. Embedded interfaces and type-set terms are
    not methods and are skipped."""
    type_node = spec.child_by_field_name("type")
    if type_node is None or type_node.type != "interface_type":
        return []
    out: list[Function] = []
    for elem in type_node.named_children:
        if elem.type != "method_elem":
            continue
        name_node = elem.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else ""
        start, end = line_span(elem)
        out.append(Function(
            id=disambiguate(function_id(path, name, start), seen_ids),
            parentId=parent_id,
            path=path,
            name=name,
            type="method",
            startLine=start,
            endLine=end,
            visibility="public" if name[:1].isupper() else "package",
            params=_extract_params(elem.child_by_field_name("parameters"), source),
            returnType=_result_type(elem, source),
        ))
    return out


def _generics(spec: Node, source: bytes) -> str | None:
    params = spec.child_by_field_name("type_parameters")
    return node_text(params, source) if params is not None else None


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
) -> tuple[list[Class], list[Function], list[Statement]]:
    if node.type != "type_declaration":
        return [], [], []
    # `type_alias` (`type ID = string`) is recorded like a named type, as `class`.
    specs = [child for child in node.named_children if child.type in {"type_spec", "type_alias"}]
    if not specs:
        return [], [], []
    classes: list[Class] = []
    methods: list[Function] = []
    for spec in specs:
        name_node = spec.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else ""
        start, end = line_span(spec)
        cid = disambiguate(class_id(path, name), seen_ids)
        classes.append(
            Class(
                id=cid,
                parentId=parent_id,
                path=path,
                name=name,
                type=_kind(spec, source),
                visibility="public" if name[:1].isupper() else "package",
                generics=_generics(spec, source),
                extends=_embedded_type(spec, source),
                implements=[],
                constructorParams=[],
                startLine=start,
                endLine=end,
            )
        )
        methods.extend(_interface_methods(spec, source, path, parent_id=cid, seen_ids=seen_ids))
    return classes, methods, []
