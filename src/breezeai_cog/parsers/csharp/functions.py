"""C# method / constructor + parameter + attribute + call extraction.

Unlike Java, C# ``modifier`` nodes are **direct children** of the declaration (there
is no ``modifiers`` wrapper), and attributes hang off ``attribute_list`` children.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

_VISIBILITY = {"public", "private", "protected", "internal", "file"}
_METHOD_TYPES = ("method_declaration", "constructor_declaration", "destructor_declaration",
                 "operator_declaration", "local_function_statement")


def flags(node: Node, source: bytes) -> tuple[str, bool]:
    """Visibility + is_static from the declaration's ``modifier`` children (default
    ``internal`` — C#'s implicit access level for types/members)."""
    visibility, is_static = "internal", False
    for child in node.children:
        if child.type == "modifier":
            m = node_text(child, source)
            if m in _VISIBILITY:
                visibility = m
            elif m == "static":
                is_static = True
    return visibility, is_static


def _attribute(node: Node, source: bytes) -> Decorator:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = next((c for c in node.named_children if c.type == "attribute_argument_list"), None)
    if arglist is not None:
        for arg in arglist.named_children:
            if arg.type != "attribute_argument":
                continue
            text = node_text(arg, source)
            inner = next((c for c in arg.named_children if c.type == "string_literal"), None)
            if inner is not None:
                text = node_text(inner, source).strip('"')
            args.append(text)
    return Decorator(name=name, args=args)


def extract_attributes(node: Node, source: bytes) -> list[Decorator]:
    """Attributes declared on a node (its ``attribute_list`` children)."""
    out: list[Decorator] = []
    for child in node.children:
        if child.type == "attribute_list":
            out.extend(_attribute(a, source) for a in child.named_children if a.type == "attribute")
    return out


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type != "parameter":
            continue
        tnode = p.child_by_field_name("type")
        nnode = p.child_by_field_name("name")
        out.append(Parameter(
            name=node_text(nnode, source) if nnode is not None else "",
            type=node_text(tnode, source) if tnode is not None else "",
            decorators=extract_attributes(p, source),
        ))
    return out


def _callee(func: Node, source: bytes) -> tuple[str, str | None]:
    """(method name, receiver) for an invocation's ``function`` node."""
    if func.type == "member_access_expression":
        name_node = func.child_by_field_name("name")
        obj = func.child_by_field_name("expression")
        name = node_text(name_node, source) if name_node is not None else ""
        return name, (node_text(obj, source) if obj is not None else None)
    return node_text(func, source), None


def _calls(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in ("class_declaration", "method_declaration", "local_function_statement",
                              "lambda_expression", "anonymous_method_expression"):
                continue
            if child.type == "invocation_expression":
                func = child.child_by_field_name("function")
                if func is not None:
                    name, receiver = _callee(func, source)
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name, path=resolve(name, receiver)))
            visit(child)

    visit(body)
    return calls


def defined_names(root: Node, source: bytes) -> set[str]:
    names: set[str] = set()
    types = set(_METHOD_TYPES) | {
        "class_declaration", "interface_declaration", "enum_declaration",
        "struct_declaration", "record_declaration",
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
    """Variable name → declared type (fields win) for same-file receiver resolution."""
    types: dict[str, str] = {}

    def add(name: str | None, typ: str | None, *, override: bool) -> None:
        if not name or not typ:
            return
        if override or name not in types:
            types[name] = typ

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "field_declaration":
                vd = next((d for d in c.named_children if d.type == "variable_declaration"), None)
                if vd is not None:
                    tnode = vd.child_by_field_name("type")
                    typ = node_text(tnode, source) if tnode is not None else None
                    for decl in vd.named_children:
                        if decl.type == "variable_declarator":
                            nm = decl.child_by_field_name("name") or (
                                decl.named_children[0] if decl.named_children else None)
                            add(node_text(nm, source) if nm is not None else None, typ, override=True)
            elif c.type == "parameter":
                tnode = c.child_by_field_name("type")
                nnode = c.child_by_field_name("name")
                add(node_text(nnode, source) if nnode is not None else None,
                    node_text(tnode, source) if tnode is not None else None, override=False)
            elif c.type == "local_declaration_statement":
                vd = next((d for d in c.named_children if d.type == "variable_declaration"), None)
                if vd is not None:
                    tnode = vd.child_by_field_name("type")
                    typ = node_text(tnode, source) if tnode is not None else None
                    for decl in vd.named_children:
                        if decl.type == "variable_declarator":
                            nm = decl.child_by_field_name("name") or (
                                decl.named_children[0] if decl.named_children else None)
                            add(node_text(nm, source) if nm is not None else None, typ, override=False)
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
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    visibility, is_static = flags(node, source)
    ret = node.child_by_field_name("returns")
    body = node.child_by_field_name("body")
    kind = "constructor" if node.type in ("constructor_declaration", "destructor_declaration") else "method"
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        visibility=visibility,
        isStatic=is_static,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_attributes(node, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements
