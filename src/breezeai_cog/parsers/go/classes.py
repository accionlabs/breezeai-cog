"""Go type extraction for struct/interface declarations."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text


def _kind(node: Node, source: bytes) -> str:
    spec = node.child_by_field_name("type_spec") or next(
        (c for c in node.named_children if c.type == "type_spec"), None
    )
    if spec is None:
        return "class"
    type_node = next((c for c in spec.named_children if c.type in {"struct_type", "interface_type"}), None)
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
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Class], list[Function], list[Statement]]:
    if node.type != "type_declaration":
        return [], [], []
    spec = node.child_by_field_name("type_spec") or next(
        (c for c in node.named_children if c.type == "type_spec"), None
    )
    if spec is None:
        return [], [], []
    name_node = spec.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(node)
    kind = _kind(node, source)
    cid = disambiguate(class_id(path, name), seen_ids)
    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        visibility="public" if name[:1].isupper() else "package",
        generics=_generics(spec, source),
        extends=_embedded_type(spec, source),
        implements=[],
        constructorParams=[],
        startLine=start,
        endLine=end,
    )
    return [cls], [], []
