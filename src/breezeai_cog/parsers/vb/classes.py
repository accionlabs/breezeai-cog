"""VB.NET class / interface / enum / struct / module extraction → Class + flat methods
+ statements.

Two VB grammar quirks handled here:
* Leading attributes (``<ApiController>``) detach from the type and sit as sibling
  ``attribute_block`` nodes *before* the ``type_declaration`` — the caller collects them
  and passes them in as ``pending_attrs``.
* ``Inherits`` / ``Implements`` don't parse into clean fields (they surface as ``ERROR`` /
  ``field_declaration``), so heritage is recovered best-effort by scanning the block's
  own source lines.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .functions import attributes_from_blocks, build_method, extract_params, flags

_TYPE = {
    "class_block": "class",
    "interface_block": "interface",
    "enum_block": "enum",
    "struct_block": "struct",
    "structure_block": "struct",
    "module_block": "module",
}
_METHOD_MEMBERS = ("method_declaration", "constructor_declaration")


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """Best-effort: scan the block's lines for ``Inherits``/``Implements`` clauses."""
    extends: str | None = None
    implements: list[str] = []
    for raw in node_text(node, source).splitlines():
        line = raw.strip()
        if line.startswith("Inherits "):
            extends = line[len("Inherits "):].split(",")[0].strip() or None
        elif line.startswith("Implements "):
            implements.extend(p.strip() for p in line[len("Implements "):].split(",") if p.strip())
    return extends, implements


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    pending_attrs: list[Node],
    resolve: CallResolver = noop_resolver,
) -> tuple[Class, list[Function], list[Statement]]:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)

    visibility, _ = flags(node, source)
    is_abstract = node.type == "interface_block" or "MustInherit" in node_text(node, source)[:200]

    methods: list[Function] = []
    statements: list[Statement] = []
    ctor_params: list[ConstructorParam] = []

    for member in node.named_children:
        if member.type in _METHOD_MEMBERS:
            fn, fn_statements = build_method(
                member, source, path,
                parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                resolve=resolve,
            )
            methods.append(fn)
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
        decorators=attributes_from_blocks(pending_attrs, source),
        startLine=start,
        endLine=end,
    )
    return cls, methods, statements
