"""VB.NET class / interface / enum / struct / module extraction → Class + flat methods
+ statements.

Three VB grammar quirks handled here:
* Leading attributes (``<ApiController>``) detach from the type and sit as sibling
  ``attribute_block`` nodes *before* the ``type_declaration`` — the caller collects them
  and passes them in as ``pending_attrs``.
* ``Inherits`` / ``Implements`` don't parse into clean fields (they surface as ``ERROR`` /
  ``field_declaration``), so heritage is recovered best-effort by scanning the block's
  own source lines.
* **Nested types are not parsed at all.** A ``Public Class Result`` inside a class surfaces
  as a ``field_declaration``, the enclosing ``class_block`` ends at the *nested* ``End
  Class``, the nested type's own members become siblings of the outer type's, and whatever
  follows lands in an ``ERROR`` node. Members after a nested type are therefore untrustworthy
  and are dropped — see :func:`_nested_type_marker`.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...logging import get_logger
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..statements_common import emit_enum_members
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

#: Type keywords that may open a nested type declaration.
_TYPE_KEYWORDS = ("Class", "Structure", "Interface", "Enum", "Module", "Delegate")
#: Modifiers that may precede one.
_TYPE_MODIFIERS = frozenset({
    "Public", "Private", "Protected", "Friend", "Shared", "Partial", "NotInheritable",
    "MustInherit", "Overloads", "Shadows",
})


def _nested_type_marker(member: Node, source: bytes) -> str | None:
    """The type name if ``member`` is really a **nested type declaration** the grammar failed
    to parse, else None.

    The grammar has no rule for a type inside a type: it emits the declaration line as a
    ``field_declaration`` and closes the enclosing ``class_block`` at the nested ``End Class``.
    Everything the grammar reports after that point is misplaced — the nested type's members
    appear as the outer type's, and the outer type's remaining members fall outside the block
    into an ``ERROR`` node. Detection reads the real declaration tokens, not a guess: a
    ``field_declaration`` whose leading words are optional modifiers followed by a type keyword.
    """
    if member.type != "field_declaration":
        return None
    words = node_text(member, source).strip().split()
    i = 0
    while i < len(words) and words[i] in _TYPE_MODIFIERS:
        i += 1
    if i < len(words) - 1 and words[i] in _TYPE_KEYWORDS:
        return words[i + 1]
    return None


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
        nested = _nested_type_marker(member, source)
        if nested is not None:
            # The grammar mis-parses from here on: the following members belong to the nested
            # type, not to this one. Attributing them here would assert a method this class
            # does not declare, so stop — a known gap beats a wrong edge.
            get_logger("breezeai_cog.parsers").debug(
                "vb.nested_type.unsupported", path=path, cls=name, nested=nested,
                line=member.start_point[0] + 1,
                reason="grammar has no nested-type rule; members after it are dropped",
            )
            break
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

    # Class-level constants and static/readonly fields (`Const`, `Shared ReadOnly`, plain
    # fields) become flat statements parented to this Class — their `text` (incl. any
    # `= value`) is the queryable business vocabulary. Method bodies are NESTED_SCOPES and
    # are extracted by build_method, so this only picks up the type's own members.
    if capture:
        from .statements import extract_statements as _extract_stmts
        statements.extend(
            _extract_stmts(node, source, path, parent_id=cid, capture=capture,
                           limit=limit, seen_ids=seen_ids)
        )

    # Enum members become flat statements parented to the enum Class (their `text` — incl.
    # any `= value` — is queryable). Gated by --capture-statements like every other statement.
    if capture and node.type == "enum_block":
        statements.extend(
            emit_enum_members(
                node, source, path,
                member_types={"enum_member"}, parent_id=cid, limit=limit, seen_ids=seen_ids,
            )
        )

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
