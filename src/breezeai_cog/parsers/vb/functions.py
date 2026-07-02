"""VB.NET method (Sub/Function) / constructor + parameter + attribute + call extraction.

VB nests modifiers under a ``modifiers`` wrapper, types under an ``as_clause``, and
attributes under ``attribute_block`` nodes; calls use ``invocation`` / ``member_access``.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

_VISIBILITY = {"Public": "public", "Private": "private", "Protected": "protected", "Friend": "internal"}


def flags(node: Node, source: bytes) -> tuple[str, bool]:
    """Visibility + is_shared from the ``modifiers`` wrapper (default ``public`` —
    VB members are Public unless stated). ``Shared`` == C#'s static."""
    visibility, is_static = "public", False
    modifiers = node.child_by_field_name("modifiers") or next(
        (c for c in node.named_children if c.type == "modifiers"), None)
    if modifiers is not None:
        for child in modifiers.children:
            m = node_text(child, source)
            if m in _VISIBILITY:
                visibility = _VISIBILITY[m]
            elif m == "Shared":
                is_static = True
    return visibility, is_static


def _attribute(node: Node, source: bytes) -> Decorator:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = next((c for c in node.named_children if c.type == "argument_list"), None)
    if arglist is not None:
        for arg in arglist.named_children:
            if arg.type != "argument":
                continue
            lit = _find_string(arg)
            args.append(node_text(lit, source).strip('"') if lit is not None else node_text(arg, source))
    return Decorator(name=name, args=args)


def _find_string(node: Node) -> Node | None:
    if node.type == "string_literal":
        return node
    for c in node.named_children:
        found = _find_string(c)
        if found is not None:
            return found
    return None


def attributes_from_blocks(blocks: list[Node], source: bytes) -> list[Decorator]:
    out: list[Decorator] = []
    for block in blocks:
        out.extend(_attribute(a, source) for a in block.named_children if a.type == "attribute")
    return out


def extract_attributes(node: Node, source: bytes) -> list[Decorator]:
    """Attributes on a declaration — its ``attributes`` (attribute_block) field/children."""
    blocks = [c for c in node.named_children if c.type == "attribute_block"]
    return attributes_from_blocks(blocks, source)


def as_clause_type(container: Node, source: bytes) -> str | None:
    """The declared type of a node's ``As <type>`` clause (param/field), else None."""
    as_clause = next((c for c in container.named_children if c.type == "as_clause"), None)
    if as_clause is not None:
        tnode = as_clause.child_by_field_name("type") or next(
            (c for c in as_clause.named_children if c.type in ("type", "primitive_type")), None)
        if tnode is not None:
            return node_text(tnode, source)
    return None


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type != "parameter":
            continue
        nnode = p.child_by_field_name("name")
        out.append(Parameter(
            name=node_text(nnode, source) if nnode is not None else "",
            type=as_clause_type(p, source) or "",
            decorators=extract_attributes(p, source),
        ))
    return out


def _calls(node: Node, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(n: Node) -> None:
        # Descend into every scope, including inline lambdas — their calls belong to
        # the nearest named enclosing function (see build_function).
        for child in n.named_children:
            if child.type == "invocation":
                target = child.child_by_field_name("target")
                name = receiver = None
                if target is not None and target.type == "member_access":
                    member = target.child_by_field_name("member")
                    obj = target.child_by_field_name("object")
                    name = node_text(member, source) if member is not None else None
                    receiver = node_text(obj, source) if obj is not None else None
                elif target is not None:
                    name = node_text(target, source)
                if name and name not in seen:
                    seen.add(name)
                    calls.append(Call(name=name, path=resolve(name, receiver)))
            visit(child)

    visit(node)
    return calls


def defined_names(root: Node, source: bytes) -> set[str]:
    names: set[str] = set()
    types = {
        "method_declaration", "constructor_declaration", "class_block",
        "interface_block", "enum_block", "struct_block", "structure_block", "module_block",
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
        if name and typ and (override or name not in types):
            types[name] = typ

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "field_declaration":
                typ = as_clause_type(c, source)
                for d in c.named_children:
                    if d.type == "variable_declarator" and d.named_children:
                        add(node_text(d.named_children[0], source), typ or as_clause_type(d, source),
                            override=True)
            elif c.type == "parameter":
                nnode = c.child_by_field_name("name")
                add(node_text(nnode, source) if nnode is not None else None,
                    as_clause_type(c, source), override=False)
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
    is_ctor = node.type == "constructor_declaration"
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ("New" if is_ctor else "<anonymous>")
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    visibility, is_static = flags(node, source)
    ret = node.child_by_field_name("return_type")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if is_ctor else "method",
        visibility=visibility,
        isStatic=is_static,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_attributes(node, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(node, source, resolve),
    )
    statements = extract_statements(
        node, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline lambdas — attribute their statements here
    )
    return fn, statements
