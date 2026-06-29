"""Java method / constructor + parameter + annotation + call extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..treesitter import line_span, node_text
from .statements import extract_statements


def modifiers_node(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "modifiers"), None)


def _flags(modifiers: Node | None) -> tuple[str, bool]:
    visibility, is_static = "package", False
    if modifiers is not None:
        for child in modifiers.children:
            if child.type in ("public", "private", "protected"):
                visibility = child.type
            elif child.type == "static":
                is_static = True
    return visibility, is_static


def _annotation(node: Node, source: bytes) -> Decorator:
    name = node_text(node.child_by_field_name("name"), source).rsplit(".", 1)[-1]
    args: list[str] = []
    arglist = node.child_by_field_name("arguments")
    if arglist is not None:
        for arg in arglist.named_children:
            text = node_text(arg, source)
            if arg.type == "string_literal":
                text = text.strip('"')
            args.append(text)
    return Decorator(name=name, args=args)


def extract_annotations(modifiers: Node | None, source: bytes) -> list[Decorator]:
    if modifiers is None:
        return []
    return [_annotation(c, source) for c in modifiers.named_children
            if c.type in ("marker_annotation", "annotation")]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type in ("formal_parameter", "spread_parameter"):
            tnode = p.child_by_field_name("type")
            nnode = p.child_by_field_name("name")
            if nnode is None:  # spread_parameter holds a variable_declarator
                decl = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                nnode = decl.child_by_field_name("name") if decl is not None else None
            out.append(Parameter(
                name=node_text(nnode, source) if nnode is not None else "",
                type=node_text(tnode, source) if tnode is not None else "",
                decorators=extract_annotations(modifiers_node(p), source),
            ))
    return out


def _calls(body: Node | None, source: bytes) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in ("class_declaration", "method_declaration", "lambda_expression"):
                continue
            if child.type == "method_invocation":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = node_text(name_node, source)
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name))
            visit(child)

    visit(body)
    return calls


def build_method(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
) -> tuple[Function, list[Statement]]:
    modifiers = modifiers_node(node)
    name = node_text(node.child_by_field_name("name"), source)
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    visibility, is_static = _flags(modifiers)
    ret = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if node.type == "constructor_declaration" else "method",
        visibility=visibility,
        isStatic=is_static,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_annotations(modifiers, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements
