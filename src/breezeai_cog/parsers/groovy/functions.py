"""Groovy method / constructor + parameter + annotation + call extraction.

Differs from Java's model in three ways the dekobon grammar dictates:
* modifiers (``public``/``private``/``static``/``abstract``/``final``/``def``) are
  **inline children** of the declaration, not wrapped in a ``modifiers`` node;
* names are a ``name`` field holding a plain ``identifier``;
* a call is a ``method_invocation`` whose ``function`` field is either a bare
  ``identifier`` (``foo()``) or a ``field_access`` (``obj.foo()``).

Default visibility in Groovy is **public** (Java's is package-private).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

_VISIBILITY = ("public", "private", "protected")


def has_declaration_error(node: Node) -> bool:
    """True if the declaration header itself is corrupt — an ``ERROR`` / missing node is a
    **direct** child of the declaration.

    This is the grammar's error-recovery tell: when a preceding construct (e.g. a
    parenthesised-constant enum body) mis-parses, dekobon merges the next field+method into
    one garbled ``method_declaration`` whose name/type/params are fabricated (a field ``ID``
    becomes a method ``ID(...)``). Such a node must NOT be emitted — capturing nothing is a
    known gap, but capturing a method that does not exist is high-confidence wrong data
    (§ reliability: absent beats wrong). A messy method *body* (an error nested deeper) is
    fine — the header is trustworthy — so we check only direct children."""
    return any(c.type == "ERROR" or c.is_missing for c in node.children)


def _flags(node: Node) -> tuple[str, bool, bool]:
    """(visibility, is_static, is_abstract) from a declaration's inline modifier children.
    Groovy defaults to public."""
    visibility, is_static, is_abstract = "public", False, False
    for child in node.children:
        if child.type in _VISIBILITY:
            visibility = child.type
        elif child.type == "static":
            is_static = True
        elif child.type == "abstract":
            is_abstract = True
    return visibility, is_static, is_abstract


def _annotation(node: Node, source: bytes) -> Decorator:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = node.child_by_field_name("arguments")
    if arglist is not None:
        for arg in arglist.named_children:
            text = node_text(arg, source)
            if arg.type == "string_literal":
                text = text.strip('"').strip("'")
            args.append(text)
    return Decorator(name=name, args=args)


def extract_annotations(node: Node, source: bytes) -> list[Decorator]:
    """Annotations are direct children of the declaration in Groovy."""
    return [_annotation(c, source) for c in node.named_children if c.type == "annotation"]


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
            decorators=extract_annotations(p, source),
        ))
    return out


def _callee(call: Node, source: bytes) -> tuple[str, str | None]:
    """(method_name, receiver) for a ``method_invocation``. ``function`` is a bare
    ``identifier`` (receiver ``None``) or a ``field_access`` (``obj.method``)."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return "", None
    if fn.type == "field_access":
        obj = fn.child_by_field_name("object")
        field = fn.child_by_field_name("field")
        method = node_text(field, source) if field is not None else ""
        receiver = node_text(obj, source) if obj is not None else None
        return method, receiver
    return node_text(fn, source), None


def _calls(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        # Descend into every scope, including inline closures — their calls belong to
        # the nearest named enclosing function (mirrors Java lambda handling).
        for child in node.named_children:
            if child.type == "method_invocation":
                name, receiver = _callee(child, source)
                if name and name not in seen:
                    seen.add(name)
                    calls.append(Call(name=name, path=resolve(name, receiver)))
            visit(child)

    visit(body)
    return calls


def _decl_types() -> set[str]:
    return {
        "method_declaration", "constructor_declaration", "class_declaration",
        "interface_declaration", "enum_declaration", "trait_declaration",
    }


def defined_names(root: Node, source: bytes) -> set[str]:
    """Method/constructor/class/interface/enum/trait names defined in the file."""
    names: set[str] = set()
    types = _decl_types()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if has_declaration_error(c):
                continue  # corrupt header → fabricated name; don't feed same-file resolution
            if c.type in types:
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Variable name → declared type, for receiver-type call resolution (Phase 2).
    Fields (the DI pattern) win over params/locals on name collisions. Untyped Groovy
    declarations (``def x`` / no type) simply contribute nothing — honest-null."""
    types: dict[str, str] = {}

    def add(name_node: Node | None, type_node: Node | None, *, override: bool) -> None:
        if name_node is None or type_node is None:
            return
        name = node_text(name_node, source)
        if override or name not in types:
            types[name] = node_text(type_node, source)

    _KEYWORD_TYPES = {"class", "interface", "enum", "trait"}

    def walk(n: Node) -> None:
        for c in n.named_children:
            if has_declaration_error(c):
                continue  # corrupt header — do not seed receiver-type resolution
            if c.type == "field_declaration":
                tnode = c.child_by_field_name("type")
                # A nested type misparsed as a field has a keyword as its "type" — inert for
                # resolution, but drop it so the type index stays honest.
                if tnode is not None and node_text(tnode, source) in _KEYWORD_TYPES:
                    continue
                for d in c.named_children:
                    if d.type == "variable_declarator":
                        add(d.child_by_field_name("name"), tnode, override=True)
            elif c.type == "formal_parameter":
                add(c.child_by_field_name("name"), c.child_by_field_name("type"), override=False)
            elif c.type == "local_variable_declaration":
                tnode = c.child_by_field_name("type")
                for d in c.named_children:
                    if d.type == "variable_declarator":
                        add(d.child_by_field_name("name"), tnode, override=False)
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
    name = node_text(node.child_by_field_name("name"), source)
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    visibility, is_static, _ = _flags(node)
    ret = node.child_by_field_name("return_type")
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
        decorators=extract_annotations(node, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline closures — attribute their statements here
    )
    return fn, statements


def build_function(
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
    """Top-level (script) method — Groovy files may declare methods outside any class."""
    return build_method(
        node, source, path, parent_id=parent_id, class_name=None,
        seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
    )
