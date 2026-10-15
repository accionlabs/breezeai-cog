"""Apex method / constructor / trigger extraction → Function + flat statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .modifiers import (
    declared_name,
    extract_annotations,
    modifier_keywords,
    modifiers_node,
    visibility_of,
)
from .statements import extract_statements


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type != "formal_parameter":
            continue
        tnode = p.child_by_field_name("type")
        nnode = p.child_by_field_name("name")
        out.append(Parameter(
            name=node_text(nnode, source) if nnode is not None else "",
            type=node_text(tnode, source) if tnode is not None else "",
            decorators=extract_annotations(modifiers_node(p), source),
        ))
    return out


def _calls(body: Node | None, source: bytes, resolve: CallResolver) -> list[Call]:
    """Distinct called method names. Apex identifiers are case-insensitive, so the resolver
    is given lower-cased names; the emitted name keeps the source spelling."""
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = node_text(name_node, source)
                obj = node.child_by_field_name("object")
                receiver = node_text(obj, source).lower() if obj is not None else None
                if name.lower() not in seen:
                    seen.add(name.lower())
                    calls.append(Call(name=name, path=resolve(name.lower(), receiver)))
        stack.extend(reversed(node.named_children))
    return calls


def defined_names(root: Node, source: bytes) -> set[str]:
    """Lower-cased method / constructor / type names declared in the file."""
    names: set[str] = set()
    types = {
        "method_declaration", "constructor_declaration", "class_declaration",
        "interface_declaration", "enum_declaration",
    }
    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.named_children:
            if child.type in types:
                name = child.child_by_field_name("name")
                if name is not None:
                    names.add(node_text(name, source).lower())
            stack.append(child)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Lower-cased variable name → declared type, for receiver-type call resolution.
    Fields win over parameters and locals on a name collision."""
    types: dict[str, str] = {}

    def add(name_node: Node | None, type_node: Node | None, *, override: bool) -> None:
        if name_node is None or type_node is None:
            return
        name = node_text(name_node, source).lower()
        if override or name not in types:
            types[name] = node_text(type_node, source).lower()

    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.named_children:
            if child.type in ("field_declaration", "local_variable_declaration"):
                for d in child.named_children:
                    if d.type == "variable_declarator":
                        add(d.child_by_field_name("name"), child.child_by_field_name("type"),
                            override=child.type == "field_declaration")
            elif child.type == "formal_parameter":
                add(child.child_by_field_name("name"), child.child_by_field_name("type"),
                    override=False)
            stack.append(child)
    return types


def build_method(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    in_interface: bool,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    modifiers = modifiers_node(node)
    keywords = modifier_keywords(modifiers)
    name = declared_name(node, source)
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    ret = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if node.type == "constructor_declaration" else "method",
        visibility=visibility_of(keywords, "public" if in_interface else "private"),
        isStatic="static" in keywords,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_annotations(modifiers, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        metadata={"webservice": True} if "webservice" in keywords else None,
        calls=_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements


def build_trigger(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    name = declared_name(node, source)
    sobject = node.child_by_field_name("object")
    events = [
        " ".join(node_text(c, source).lower().split())
        for c in node.named_children
        if c.type == "trigger_event"
    ]
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start), seen_ids)
    body = node.child_by_field_name("body")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="trigger",
        startLine=start,
        endLine=end,
        metadata={
            "sObject": node_text(sobject, source) if sobject is not None else None,
            "events": events,
        },
        calls=_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements
