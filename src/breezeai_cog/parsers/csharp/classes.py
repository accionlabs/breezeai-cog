"""C# class / interface / enum / struct / record extraction → Class + flat methods
+ statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .functions import build_method, extract_attributes, extract_params, flags
from .statements import extract_statements

_TYPE = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "struct_declaration": "struct",
    "record_declaration": "record",
}
_METHOD_MEMBERS = ("method_declaration", "constructor_declaration",
                   "destructor_declaration", "operator_declaration")
_NESTED_CLASS_TYPES = tuple(_TYPE)  # member (nested) types declared in a type body


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """Split ``base_list`` into (extends, implements). C# lists a single base class
    first (if any) followed by interfaces; interface names conventionally start ``I``,
    which we use to disambiguate the first entry."""
    base = node.child_by_field_name("base_list") or next(
        (c for c in node.named_children if c.type == "base_list"), None)
    if base is None:
        return None, []
    names = [node_text(c, source) for c in base.named_children
             if c.type in ("identifier", "qualified_name", "generic_name")]
    if not names:
        return None, []
    first = names[0]
    short = first.rsplit(".", 1)[-1]
    if len(short) >= 2 and short[0] == "I" and short[1].isupper():
        return None, names  # all interfaces
    return first, names[1:]


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
) -> tuple[Class, list[Function], list[Statement]]:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)

    visibility, _ = flags(node, source)
    is_abstract = node.type == "interface_declaration" or any(
        c.type == "modifier" and node_text(c, source) == "abstract" for c in node.children)

    methods: list[Function] = []
    statements: list[Statement] = []
    ctor_params: list[ConstructorParam] = []

    # record positional parameters (``record Money(decimal Amount)``) → constructorParams
    param_list = node.child_by_field_name("parameters") or next(
        (c for c in node.named_children if c.type == "parameter_list"), None)
    if param_list is not None:
        ctor_params = [ConstructorParam(name=p.name, type=p.type)
                       for p in extract_params(param_list, source)]

    body = node.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        for member in body.named_children:
            if member.type in _METHOD_MEMBERS:
                fns, fn_statements = build_method(
                    member, source, path,
                    parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                    resolve=resolve,
                )
                methods.extend(fns)
                statements.extend(fn_statements)
                if member.type == "constructor_declaration" and not ctor_params:
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(member.child_by_field_name("parameters"), source)
                    ]

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_TYPE.get(node.type, "class"),
        visibility=visibility,
        isAbstract=is_abstract,
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        decorators=extract_attributes(node, source),
        startLine=start,
        endLine=end,
    )
    return cls, methods, statements
