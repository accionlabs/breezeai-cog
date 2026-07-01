"""Java method / constructor + parameter + annotation + call extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
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


def _calls(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
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
                    obj = child.child_by_field_name("object")
                    receiver = node_text(obj, source) if obj is not None else None
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name, path=resolve(name, receiver)))
            visit(child)

    visit(body)
    return calls


def defined_names(root: Node, source: bytes) -> set[str]:
    """Method/constructor/class/interface/enum/record names defined in the file."""
    names: set[str] = set()
    types = {
        "method_declaration", "constructor_declaration", "class_declaration",
        "interface_declaration", "enum_declaration", "record_declaration",
    }

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in types:
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Variable name → declared type, for receiver-type call resolution (Phase 2).
    Fields (the DI pattern) win over params/locals on name collisions."""
    types: dict[str, str] = {}

    def add(name_node: Node | None, type_node: Node | None, *, override: bool) -> None:
        if name_node is None or type_node is None:
            return
        name = node_text(name_node, source)
        if override or name not in types:
            types[name] = node_text(type_node, source)

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "field_declaration":
                for d in c.named_children:
                    if d.type == "variable_declarator":
                        add(d.child_by_field_name("name"), c.child_by_field_name("type"), override=True)
            elif c.type == "formal_parameter":
                add(c.child_by_field_name("name"), c.child_by_field_name("type"), override=False)
            elif c.type == "local_variable_declaration":
                for d in c.named_children:
                    if d.type == "variable_declarator":
                        add(d.child_by_field_name("name"), c.child_by_field_name("type"), override=False)
            walk(c)

    walk(root)
    return types


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
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    modifiers = modifiers_node(node)
    name = node_text(node.child_by_field_name("name"), source)
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    visibility, is_static = _flags(modifiers)
    ret = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    tp = node.child_by_field_name("type_parameters")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if node.type == "constructor_declaration" else "method",
        visibility=visibility,
        isStatic=is_static,
        generics=node_text(tp, source) if tp is not None else None,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_annotations(modifiers, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements
